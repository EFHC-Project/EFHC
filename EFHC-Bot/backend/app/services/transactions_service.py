# -*- coding: utf-8 -*-
# backend/app/services/transactions_service.py
# =============================================================================
# EFHC Bot — Банковский сервис (канон, READ-THROUGH идемпотентность)
# -----------------------------------------------------------------------------
# ЕДИНСТВЕННАЯ точка входа для любых денежных движений EFHC.
# Обязательные функции (канон):
#   • credit_user_from_bank(...)         — кредит на ОСНОВНОЙ баланс
#   • credit_user_bonus_from_bank(...)   — кредит на БОНУСНЫЙ баланс
#   • debit_user_to_bank(...)            — дебет с ОСНОВНОГО баланса
#   • debit_user_bonus_to_bank(...)      — дебет с БОНУСНОГО баланса
#   • exchange_kwh_to_efhc(...)          — кредит EFHC 1:1 за энергию
#
# Правила:
#   • У пользователя ЗАПРЕЩЁН отрицательный баланс (main/bonus).
#   • У Банка РАЗРЕШЁН отрицательный баланс (дефицит) — операции не блокируются.
#   • Любая операция — идемпотентна по idempotency_key (UNIQUE в журнале).
#   • Decimal(30,8), округление вниз; единая функция deps.d8().
#   • «Зеркальный» лог Банка записывается с ключом mirror:<idempotency_key>.
#   • Никаких P2P — только «пользователь ↔ банк».
#
# ИИ-защита/самовосстановление:
#   • READ-THROUGH: нет предварительного SELECT по idempotency_key — полная атомарность.
#   • При конфликте UNIQUE(idempotency_key) — откат и возврат результата уже записанной операции
#     (при этом в extra_info действующей записи помечаем idk_conflict="true").
#   • Мягкие ретраи для deadlock/serialize конфликтов.
#   • Идемпотентность позволяет безопасно повторять вызовы без дублей и искажений.
# =============================================================================

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional, Tuple, Callable, Awaitable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config_core import get_settings
from backend.app.core.logging_core import get_logger
from backend.app.deps import d8  # единое округление до 8 знаков (округление вниз)

logger = get_logger(__name__)
settings = get_settings()
SCHEMA = getattr(settings, "DB_SCHEMA_CORE", "efhc_core")

# Telegram ID банковского аккаунта (из .env)
BANK_TG_ID = int(getattr(settings, "ADMIN_BANK_TELEGRAM_ID", "0") or "0")
if BANK_TG_ID <= 0:
    logger.warning("ADMIN_BANK_TELEGRAM_ID не задан — зеркальные логи банка будут недоступны.")

# -----------------------------------------------------------------------------
# DTO результата операции
# -----------------------------------------------------------------------------

@dataclass
class TxResult:
    ok: bool
    user_id: int
    amount: Decimal
    balance_type: str           # "main" | "bonus"
    reason: str
    idempotency_key: str
    user_main_balance: Decimal  # финальный снимок после операции
    user_bonus_balance: Decimal
    bank_main_balance: Optional[Decimal] = None
    created_log_id: Optional[int] = None
    detail: str = "ok"
    processed_with_deficit: bool = False  # True, если банк ушёл в минус в результате операции

# -----------------------------------------------------------------------------
# Низкоуровневые SQL (осознанно без ORM, ради прозрачности)
# -----------------------------------------------------------------------------

_GET_USER_FOR_UPDATE_SQL = text(
    f"""
    SELECT telegram_id,
           COALESCE(main_balance, 0)  AS main_balance,
           COALESCE(bonus_balance, 0) AS bonus_balance
      FROM {SCHEMA}.users
     WHERE telegram_id = :uid
     FOR UPDATE
    """
)

_UPSERT_USER_SQL = text(
    f"""
    INSERT INTO {SCHEMA}.users
      (telegram_id, username, is_vip, is_active, main_balance, bonus_balance,
       total_generated_kwh, available_kwh, created_at, updated_at)
    VALUES (:uid, :username, FALSE, TRUE, 0, 0, 0, 0, NOW(), NOW())
    ON CONFLICT (telegram_id) DO NOTHING
    """
)

_UPDATE_USER_BALANCES_SQL = text(
    f"""
    UPDATE {SCHEMA}.users
       SET main_balance  = :mb,
           bonus_balance = :bb,
           updated_at    = NOW()
     WHERE telegram_id   = :uid
    """
)

_INSERT_LOG_SQL = text(
    f"""
    INSERT INTO {SCHEMA}.efhc_transfers_log
      (user_id, amount, direction, balance_type, reason, idempotency_key, created_at, extra_info)
    VALUES
      (:user_id, :amount, :direction, :balance_type, :reason, :idk, NOW(),
       COALESCE(:extra_info::jsonb, '{{}}'::jsonb))
    RETURNING id
    """
)

_SELECT_LOG_BY_IDK_SQL = text(
    f"""
    SELECT id, user_id, amount, direction, balance_type, reason, idempotency_key, created_at
      FROM {SCHEMA}.efhc_transfers_log
     WHERE idempotency_key = :idk
     LIMIT 1
    """
)

_MARK_IDK_CONFLICT_SQL = text(
    f"""
    UPDATE {SCHEMA}.efhc_transfers_log
       SET extra_info = COALESCE(extra_info,'{{}}'::jsonb) || '{{"idk_conflict":"true"}}'::jsonb
     WHERE idempotency_key = :idk
    """
)

# -----------------------------------------------------------------------------
# Утилиты блокировки пользователя и фиксации балансов
# -----------------------------------------------------------------------------

async def _ensure_user_locked(db: AsyncSession, user_id: int) -> Tuple[Decimal, Decimal]:
    """
    Гарантированно возвращает (main_balance, bonus_balance) с FOR UPDATE.
    Если пользователя нет — создаём пустую карточку и блокируем.
    ВНИМАНИЕ: user_id здесь трактуется как telegram_id (по канону этого сервиса).
    """
    res = await db.execute(_GET_USER_FOR_UPDATE_SQL, {"uid": int(user_id)})
    row = res.fetchone()
    if row:
        return d8(row[1] or 0), d8(row[2] or 0)

    await db.execute(_UPSERT_USER_SQL, {"uid": int(user_id), "username": None})
    res2 = await db.execute(_GET_USER_FOR_UPDATE_SQL, {"uid": int(user_id)})
    row2 = res2.fetchone()
    if not row2:
        raise RuntimeError(f"Не удалось создать/заблокировать пользователя uid={user_id}")
    return d8(row2[1] or 0), d8(row2[2] or 0)

async def _update_user_balances(db: AsyncSession, user_id: int, main_balance: Decimal, bonus_balance: Decimal) -> None:
    await db.execute(
        _UPDATE_USER_BALANCES_SQL,
        {
            "uid": int(user_id),
            "mb": str(d8(main_balance)),
            "bb": str(d8(bonus_balance)),
        },
    )

async def _insert_log(
    db: AsyncSession,
    *,
    user_id: int,
    amount: Decimal,
    direction: str,
    balance_type: str,
    reason: str,
    idk: str,
    extra_info: Optional[dict] = None,
) -> int:
    res = await db.execute(
        _INSERT_LOG_SQL,
        {
            "user_id": int(user_id),
            "amount": str(d8(amount)),
            "direction": direction,
            "balance_type": balance_type,
            "reason": reason,
            "idk": idk,
            "extra_info": json.dumps(extra_info or {}),
        },
    )
    rid = res.fetchone()[0]
    return int(rid)

async def _find_log_by_idk(db: AsyncSession, idk: str) -> Optional[dict]:
    res = await db.execute(_SELECT_LOG_BY_IDK_SQL, {"idk": idk})
    row = res.fetchone()
    if not row:
        return None
    return {
        "id": int(row[0]),
        "user_id": (int(row[1]) if row[1] is not None else None),
        "amount": Decimal(str(row[2])),
        "direction": str(row[3]),
        "balance_type": str(row[4]),
        "reason": str(row[5]),
        "idempotency_key": str(row[6]),
        "created_at": row[7],
    }

# -----------------------------------------------------------------------------
# READ-THROUGH обвязка идемпотентной операции
# -----------------------------------------------------------------------------

RunCore = Callable[..., Awaitable[Tuple[Decimal, Decimal, Decimal, bool]]]

async def _run_idempotent(
    db: AsyncSession,
    *,
    idempotency_key: str,
    user_id: int,
    balance_type: str,   # "main" | "bonus"
    amount: Decimal,
    reason: str,
    direction: str,      # "bank_to_user" | "user_to_bank"
    exec_core: RunCore,
) -> TxResult:
    """
    exec_core: async fn(db, uid, bank_id, user_main, user_bonus, bank_main, bank_bonus,
                        amount, balance_type, reason, idempotency_key)
               -> (new_user_main, new_user_bonus, new_bank_main, processed_with_deficit)
    """
    max_tries = 3
    backoff = 0.15
    last_exc: Optional[Exception] = None

    for attempt in range(1, max_tries + 1):
        try:
            await db.begin()

            # Эксклюзивно блокируем пользователя и банк
            user_main, user_bonus = await _ensure_user_locked(db, user_id)

            bank_main = Decimal("0")
            bank_bonus = Decimal("0")
            if BANK_TG_ID > 0:
                bank_main, bank_bonus = await _ensure_user_locked(db, BANK_TG_ID)

            # Считаем новые балансы ядром
            new_user_main, new_user_bonus, new_bank_main, deficit = await exec_core(
                db=db,
                uid=int(user_id),
                bank_id=(int(BANK_TG_ID) if BANK_TG_ID > 0 else None),
                user_main=user_main,
                user_bonus=user_bonus,
                bank_main=bank_main,
                bank_bonus=bank_bonus,
                amount=d8(amount),
                balance_type=balance_type,
                reason=reason,
                idempotency_key=idempotency_key,
            )

            # Жёсткий инвариант: пользователь не может уйти в минус
            if new_user_main < Decimal("0") or new_user_bonus < Decimal("0"):
                raise ValueError("User balance negative is forbidden by canon")

            # Фиксируем новые балансы
            await _update_user_balances(db, user_id, new_user_main, new_user_bonus)
            if BANK_TG_ID > 0:
                await _update_user_balances(db, BANK_TG_ID, new_bank_main, bank_bonus)

            # Пишем основной лог (direction задаётся явно из бизнес-операции)
            extra_info_user = {}
            if deficit:
                extra_info_user["bank_deficit_mode"] = "true"

            log_id = await _insert_log(
                db,
                user_id=int(user_id),
                amount=d8(amount),
                direction=direction,                # "bank_to_user" | "user_to_bank"
                balance_type=balance_type,
                reason=reason,
                idk=idempotency_key,
                extra_info=extra_info_user,
            )

            # Зеркальный лог для банка (идемпотентен по mirror:<idk>)
            if BANK_TG_ID > 0:
                mirror_key = f"mirror:{idempotency_key}"
                mirror_direction = f"{direction}_mirror"
                extra_info_bank = {
                    "mirror_for_user": int(user_id),
                    "mirror_of_idk": idempotency_key,
                }
                try:
                    await _insert_log(
                        db,
                        user_id=int(BANK_TG_ID),
                        amount=d8(amount),
                        direction=mirror_direction,
                        balance_type="main",
                        reason=f"mirror:{reason}",
                        idk=mirror_key,
                        extra_info=extra_info_bank,
                    )
                except Exception as me:
                    if "unique" not in str(me).lower():
                        logger.warning("Mirror log insert error (non-unique or other): %s", me)

            await db.commit()

            return TxResult(
                ok=True,
                user_id=int(user_id),
                amount=d8(amount),
                balance_type=balance_type,
                reason=reason,
                idempotency_key=idempotency_key,
                user_main_balance=d8(new_user_main),
                user_bonus_balance=d8(new_user_bonus),
                bank_main_balance=(d8(new_bank_main) if BANK_TG_ID > 0 else None),
                created_log_id=int(log_id),
                processed_with_deficit=bool(deficit),
                detail="ok",
            )

        except Exception as e:
            # Откатываем текущую попытку
            try:
                await db.rollback()
            except Exception:
                pass

            msg = str(e).lower()

            # 1) Конфликт идемпотентности — читаем существующую запись и возвращаем «replay»
            if ("unique" in msg) and ("idempotency_key" in msg):
                # помечаем запись как имевшую конфликт идемпотентности
                try:
                    await db.execute(_MARK_IDK_CONFLICT_SQL, {"idk": idempotency_key})
                    await db.commit()
                except Exception:
                    try:
                        await db.rollback()
                    except Exception:
                        pass

                prev = await _find_log_by_idk(db, idempotency_key)
                if prev:
                    # Текущий снимок балансов пользователя (после уже завершённой операции)
                    um, ub = await _ensure_user_locked(db, user_id)
                    try:
                        await db.rollback()
                    except Exception:
                        pass
                    return TxResult(
                        ok=True,
                        user_id=int(user_id),
                        amount=d8(prev["amount"]),
                        balance_type=prev["balance_type"],
                        reason=prev["reason"],
                        idempotency_key=idempotency_key,
                        user_main_balance=d8(um),
                        user_bonus_balance=d8(ub),
                        bank_main_balance=None,
                        created_log_id=int(prev["id"]),
                        detail="idempotent_replay_read_through",
                        processed_with_deficit=False,
                    )
                # Уникальность есть, но запись не прочиталась — короткий retry
                await asyncio.sleep(backoff * attempt)
                last_exc = e
                continue

            # 2) Мягкие БД-коллизии — делаем короткий backoff и повторяем
            if ("deadlock" in msg) or ("could not serialize" in msg) or ("serialization" in msg) or ("conflict" in msg):
                await asyncio.sleep(backoff * attempt)
                last_exc = e
                continue

            # 3) Иное — критическая ошибка
            last_exc = e
            logger.error("Bank tx fatal error (attempt %s/%s): %s", attempt, max_tries, e)
            break

    raise RuntimeError(f"Bank transaction failed after {max_tries} attempts: {last_exc}")

# -----------------------------------------------------------------------------
# Бизнес-операции (канон)
# -----------------------------------------------------------------------------
# ВАЖНО: для совместимости поддерживаем amount_efhc/amount и amount_kwh/amount.

async def credit_user_from_bank(
    db: AsyncSession,
    *,
    user_id: int,
    amount_efhc: Optional[Decimal] = None,
    amount: Optional[Decimal] = None,
    reason: str,
    idempotency_key: str,
) -> TxResult:
    """
    Начисление пользователю EFHC из Банка на ОСНОВНОЙ баланс (main += amount).
    Банк может уйти в минус — это допустимо (дефицит).

    Аргументы суммы:
      • новый стиль:  amount=Decimal(...)
      • старый стиль: amount_efhc=Decimal(...)
    """
    if amount is None and amount_efhc is None:
        raise ValueError("Необходимо указать amount или amount_efhc")
    if amount is None:
        amount = amount_efhc

    amt = d8(amount)
    if amt <= Decimal("0"):
        raise ValueError("Сумма должна быть > 0")

    async def _core(**kwargs):
        user_main: Decimal = kwargs["user_main"]
        user_bonus: Decimal = kwargs["user_bonus"]
        bank_main: Decimal = kwargs["bank_main"]
        amount_local: Decimal = kwargs["amount"]

        new_user_main = d8(user_main + amount_local)
        new_user_bonus = user_bonus
        new_bank_main  = d8(bank_main - amount_local)  # банк может стать отрицательным
        deficit = new_bank_main < Decimal("0")
        return (new_user_main, new_user_bonus, new_bank_main, deficit)

    return await _run_idempotent(
        db,
        idempotency_key=idempotency_key,
        user_id=int(user_id),
        balance_type="main",
        amount=amt,
        reason=reason,               # рекомендуем: "credit_main:..."
        direction="bank_to_user",
        exec_core=_core,
    )

async def credit_user_bonus_from_bank(
    db: AsyncSession,
    *,
    user_id: int,
    amount_efhc: Optional[Decimal] = None,
    amount: Optional[Decimal] = None,
    reason: str,
    idempotency_key: str,
    meta: Optional[dict] = None,
) -> TxResult:
    """
    Начисление пользователю EFHC из Банка на БОНУСНЫЙ баланс (bonus += amount).
    Используется для наград за задания, промо-бонусов и т.п. (условно невыплатные монеты).
    Банк может уйти в минус — это допустимо (дефицит). Пользователь не может в минус.
    """
    if amount is None and amount_efhc is None:
        raise ValueError("Необходимо указать amount или amount_efhc")
    if amount is None:
        amount = amount_efhc

    amt = d8(amount)
    if amt <= Decimal("0"):
        raise ValueError("Сумма должна быть > 0")

    async def _core(**kwargs):
        user_main: Decimal = kwargs["user_main"]
        user_bonus: Decimal = kwargs["user_bonus"]
        bank_main: Decimal = kwargs["bank_main"]
        amount_local: Decimal = kwargs["amount"]

        new_user_main  = user_main
        new_user_bonus = d8(user_bonus + amount_local)   # кредит в BONUS
        new_bank_main  = d8(bank_main - amount_local)    # банк уменьшается (может стать < 0)
        deficit = new_bank_main < Decimal("0")
        return (new_user_main, new_user_bonus, new_bank_main, deficit)

    # reason должен ясно сигнализировать, что это кредит/награда —
    # пример: "task_reward:join_channel", "referral_activation", "promo_bonus"
    result = await _run_idempotent(
        db,
        idempotency_key=idempotency_key,
        user_id=int(user_id),
        balance_type="bonus",
        amount=amt,
        reason=reason,
        direction="bank_to_user",
        exec_core=_core,
    )

    # meta можно при необходимости дополнительно протоколировать во внешних логах/аудите
    if meta:
        logger.debug("credit_user_bonus_from_bank meta=%s", meta)

    return result

async def debit_user_to_bank(
    db: AsyncSession,
    *,
    user_id: int,
    amount_efhc: Optional[Decimal] = None,
    amount: Optional[Decimal] = None,
    reason: str,
    idempotency_key: str,
) -> TxResult:
    """
    Списание EFHC из ОСНОВНОГО баланса пользователя в Банк (main -= amount).
    Пользователь не может уйти в минус.
    """
    if amount is None and amount_efhc is None:
        raise ValueError("Необходимо указать amount или amount_efhc")
    if amount is None:
        amount = amount_efhc

    amt = d8(amount)
    if amt <= Decimal("0"):
        raise ValueError("Сумма должна быть > 0")

    async def _core(**kwargs):
        user_main: Decimal = kwargs["user_main"]
        user_bonus: Decimal = kwargs["user_bonus"]
        bank_main: Decimal = kwargs["bank_main"]
        amount_local: Decimal = kwargs["amount"]

        if user_main - amount_local < Decimal("0"):
            raise ValueError("Недостаточно средств на основном балансе пользователя")

        new_user_main = d8(user_main - amount_local)
        new_user_bonus = user_bonus
        new_bank_main  = d8(bank_main + amount_local)
        deficit = new_bank_main < Decimal("0")
        return (new_user_main, new_user_bonus, new_bank_main, deficit)

    return await _run_idempotent(
        db,
        idempotency_key=idempotency_key,
        user_id=int(user_id),
        balance_type="main",
        amount=amt,
        reason=reason,              # рекомендуем: "panel_purchase_main", "withdraw_request", ...
        direction="user_to_bank",
        exec_core=_core,
    )

async def debit_user_bonus_to_bank(
    db: AsyncSession,
    *,
    user_id: int,
    amount_efhc: Optional[Decimal] = None,
    amount: Optional[Decimal] = None,
    reason: str,
    idempotency_key: str,
) -> TxResult:
    """
    Списание EFHC из БОНУСНОГО баланса пользователя в Банк (bonus -= amount).
    Пользователь не может уйти в минус по бонусам.
    """
    if amount is None and amount_efhc is None:
        raise ValueError("Необходимо указать amount или amount_efhc")
    if amount is None:
        amount = amount_efhc

    amt = d8(amount)
    if amt <= Decimal("0"):
        raise ValueError("Сумма должна быть > 0")

    async def _core(**kwargs):
        user_main: Decimal = kwargs["user_main"]
        user_bonus: Decimal = kwargs["user_bonus"]
        bank_main: Decimal = kwargs["bank_main"]
        amount_local: Decimal = kwargs["amount"]

        if user_bonus - amount_local < Decimal("0"):
            raise ValueError("Недостаточно средств на бонусном балансе пользователя")

        new_user_main = user_main
        new_user_bonus = d8(user_bonus - amount_local)
        new_bank_main  = d8(bank_main + amount_local)
        deficit = new_bank_main < Decimal("0")
        return (new_user_main, new_user_bonus, new_bank_main, deficit)

    return await _run_idempotent(
        db,
        idempotency_key=idempotency_key,
        user_id=int(user_id),
        balance_type="bonus",
        amount=amt,
        reason=reason,              # рекомендуем: "panel_purchase_bonus", ...
        direction="user_to_bank",
        exec_core=_core,
    )

async def exchange_kwh_to_efhc(
    db: AsyncSession,
    *,
    user_id: int,
    amount_kwh: Optional[Decimal] = None,
    amount: Optional[Decimal] = None,
    reason: str,
    idempotency_key: str,
    strict_kwh_check: bool = False,
) -> TxResult:
    """
    Конвертация энергии → EFHC (1:1) с зачислением в ОСНОВНОЙ баланс.
    ВАЖНО: уменьшение available_kwh выполняет exchange_service;
    здесь — только кредит EFHC из Банка по курсу 1:1.

    Аргументы суммы:
      • новый стиль:  amount=Decimal(...)
      • старый стиль: amount_kwh=Decimal(...)
    """
    if amount is None and amount_kwh is None:
        raise ValueError("Необходимо указать amount или amount_kwh")
    if amount is None:
        amount = amount_kwh

    amt = d8(amount)
    if amt <= Decimal("0"):
        raise ValueError("Сумма должна быть > 0")

    async def _core(**kwargs):
        uid: int = kwargs["uid"]
        user_main: Decimal = kwargs["user_main"]
        user_bonus: Decimal = kwargs["user_bonus"]
        bank_main: Decimal = kwargs["bank_main"]
        amount_local: Decimal = kwargs["amount"]

        if strict_kwh_check:
            row = await db.execute(
                text(f"SELECT COALESCE(available_kwh,0) FROM {SCHEMA}.users WHERE telegram_id = :uid"),
                {"uid": int(uid)},
            )
            ak = Decimal(str(row.scalar() or "0"))
            if ak < amount_local:
                raise ValueError("Недостаточно доступной энергии для обмена (strict_kwh_check)")

        new_user_main = d8(user_main + amount_local)  # 1:1
        new_user_bonus = user_bonus
        new_bank_main  = d8(bank_main - amount_local)  # банк может уйти в минус
        deficit = new_bank_main < Decimal("0")
        return (new_user_main, new_user_bonus, new_bank_main, deficit)

    return await _run_idempotent(
        db,
        idempotency_key=idempotency_key,
        user_id=int(user_id),
        balance_type="main",
        amount=amt,
        reason=reason,              # рекомендуем: "exchange_kwh_to_efhc"
        direction="bank_to_user",
        exec_core=_core,
    )

# -----------------------------------------------------------------------------
# Доп. утилита (для роутов/админки)
# -----------------------------------------------------------------------------

async def get_user_balances_snapshot(db: AsyncSession, *, user_id: int) -> Tuple[Decimal, Decimal]:
    """
    Возвращает срез (main_balance, bonus_balance) БЕЗ блокировки и изменений.
    Только для отображения/отчётности.
    user_id трактуется как telegram_id.
    """
    row = await db.execute(
        text(
            f"""
            SELECT COALESCE(main_balance,0), COALESCE(bonus_balance,0)
              FROM {SCHEMA}.users
             WHERE telegram_id = :uid
            """
        ),
        {"uid": int(user_id)},
    )
    rec = row.fetchone()
    if not rec:
        return (Decimal("0"), Decimal("0"))
    return d8(rec[0] or 0), d8(rec[1] or 0)

# =============================================================================
# Пояснения «для чайника»:
#   • READ-THROUGH: мы НЕ проверяем лог заранее. Если параллельная операция уже
#     записала idempotency_key, мы поймаем UNIQUE, отметим extra_info.idk_conflict,
#     откатим транзакцию и вернём результат существующей записи — это безопасно.
#   • Пользователь НИКОГДА не уходит в минус; Банк МОЖЕТ — и это не блокирует поток.
#   • direction в логах:
#       - "bank_to_user"   — банк → пользователь (credit/обмен энергии),
#       - "user_to_bank"   — пользователь → банк (покупки, выводы),
#       - "*_mirror"       — зеркальные записи для BANK_TG_ID (для аудита, не для метрик).
#   • Для бонусных начислений используйте credit_user_bonus_from_bank(...).
#   • Для внешних сервисов (tasks, referrals, shop) всегда передавайте idempotency_key,
#     который стабильно повторяется при ретраях.
# =============================================================================
