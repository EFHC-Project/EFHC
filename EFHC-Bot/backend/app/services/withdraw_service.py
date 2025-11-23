# -*- coding: utf-8 -*-
# backend/app/services/withdraw_service.py
# =============================================================================
# EFHC Bot — сервис заявок на вывод EFHC
# -----------------------------------------------------------------------------
# Назначение:
#   • Обработка заявок на вывод EFHC (ТОЛЬКО EFHC, не TON). Бонусы не выводятся.
#   • При создании заявки мгновенно «холдируем» сумму: списываем EFHC у пользователя
#     в Банк (debit_user_to_bank) — чтобы исключить двойное расходование.
#   • Админ утверждает/отклоняет; при отмене/отклонении делается рефанд из Банка.
#   • Строгая идемпотентность:
#       - по client_idk (Idempotency-Key клиента) для заявок;
#       - по idempotency_key в банковских операциях.
#
# Канон/инварианты:
#   • Пользователь НЕ может уходить в минус по балансу.
#   • Банк может быть в минусе — это допустимо, операции не блокируются.
#   • Все денежные движения только через банковский сервис transactions_service.
#   • Вывод возможен только с основного баланса (main_balance), бонусы не выводятся.
#
# ИИ-защиты:
#   • read-through: повторный client_idk возвращает существующую заявку.
#   • Банковские операции идемпотентны — повтор по idempotency_key безопасен.
#   • ensure_consistency(): авто-ремонт «висящих» заявок (без холда/без рефанда).
#   • Любые сбои логируются, модуль не ломает общий цикл.
#
# Запреты:
#   • Нет P2P, нет обратной конверсии EFHC→kWh.
#   • Нет автоматической внешней доставки EFHC — только статус PAID по факту
#     ручной операции админом/интеграцией (а не в этом модуле).
# =============================================================================

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Any, Dict, Optional, Tuple, List

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config_core import get_settings
from backend.app.core.logging_core import get_logger
from backend.app.deps import d8
from backend.app.services.transactions_service import (
    debit_user_to_bank,    # списать EFHC с пользователя → в Банк (холд)
    credit_user_from_bank, # вернуть EFHC пользователю из Банка (рефанд)
)

logger = get_logger(__name__)
settings = get_settings()
SCHEMA = getattr(settings, "DB_SCHEMA_CORE", "efhc_core")

# -----------------------------------------------------------------------------
# Статусы заявок
# -----------------------------------------------------------------------------
WITHDRAW_REQ_REQUESTED = "REQUESTED"     # создана, сумма уже «в холде» в Банке
WITHDRAW_REQ_APPROVED = "APPROVED"      # админ утвердил выплату (внешняя часть не здесь)
WITHDRAW_REQ_REJECTED = "REJECTED"      # админ отклонил → рефанд выполнен
WITHDRAW_REQ_CANCELED = "CANCELED"      # пользователь отменил → рефанд выполнен
WITHDRAW_REQ_PAID     = "PAID"          # выплата завершена админом (внешне), холд остаётся в Банке

# -----------------------------------------------------------------------------
# DTO
# -----------------------------------------------------------------------------
@dataclass
class WithdrawRequestDTO:
    id: int
    user_id: int
    amount: Decimal
    status: str
    client_idk: str                        # уникальный ключ идемпотентности клиента
    hold_done: bool
    refund_done: bool
    payout_ref: Optional[str]
    created_at: str
    updated_at: str


@dataclass
class WithdrawPageDTO:
    items: List[WithdrawRequestDTO]
    next_cursor: Optional[str]


# =============================================================================
# Помощники
# =============================================================================

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _row_to_dto(row) -> WithdrawRequestDTO:
    return WithdrawRequestDTO(
        id=int(row[0]),
        user_id=int(row[1]),
        amount=d8(row[2]),
        status=str(row[3]),
        client_idk=str(row[4]),
        hold_done=bool(row[5]),
        refund_done=bool(row[6]),
        payout_ref=(str(row[7]) if row[7] else None),
        created_at=row[8].astimezone(timezone.utc).isoformat(),
        updated_at=row[9].astimezone(timezone.utc).isoformat(),
    )


def _encode_cursor(ts: datetime, rid: int) -> str:
    import json
    return json.dumps(
        {
            "ts": ts.astimezone(timezone.utc).isoformat(),
            "id": int(rid),
        },
        separators=(",", ":"),
    )


def _decode_cursor(cursor: Optional[str]) -> Tuple[Optional[datetime], Optional[int]]:
    if not cursor:
        return None, None
    try:
        import json

        data = json.loads(cursor)
        ts_raw = data.get("ts")
        rid = data.get("id")
        ts = None
        if ts_raw:
            ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00")).astimezone(timezone.utc)
        return ts, (int(rid) if rid is not None else None)
    except Exception:
        return None, None


# =============================================================================
# Создание заявки с мгновенным холдом (idempotent по client_idk)
#
# Таблица withdraw_requests (ожидается миграцией):
#   id BIGSERIAL PK,
#   user_id BIGINT NOT NULL,
#   amount NUMERIC(30,8) NOT NULL,
#   status TEXT NOT NULL,
#   client_idk TEXT NOT NULL UNIQUE,
#   hold_done BOOLEAN NOT NULL DEFAULT FALSE,
#   refund_done BOOLEAN NOT NULL DEFAULT FALSE,
#   payout_ref TEXT NULL,
#   created_at timestamptz NOT NULL DEFAULT now(),
#   updated_at timestamptz NOT NULL DEFAULT now()
# =============================================================================

async def request_withdraw(
    db: AsyncSession,
    *,
    user_id: int,
    amount: Decimal,
    client_idk: str,
) -> WithdrawRequestDTO:
    """
    Создаёт заявку REQUESTED и «холдирует» сумму: списывает EFHC у пользователя
    в Банк (debit_user_to_bank) — чтобы исключить двойное расходование.

    Вход:
      • amount     — Decimal > 0, только из основного баланса.
      • client_idk — Idempotency-Key клиента (строго уникален).

    Идемпотентность:
      • UNIQUE(client_idk): повтор вызова вернёт существующую заявку.
      • Холд через банковский сервис с idempotency_key=f"{client_idk}:hold".

    Исключения:
      • ValueError — если некорректные параметры.
      • RuntimeError — если недостаточно средств или временная ошибка холда.
    """
    amt = d8(amount)
    if amt <= 0:
        raise ValueError("amount must be positive")

    # 1) Создаём (или достаём) заявку. Если client_idk уже есть — вернётся старая запись.
    try:
        sql = text(
            f"""
            INSERT INTO {SCHEMA}.withdraw_requests
              (user_id, amount, status, client_idk)
            VALUES
              (:uid, :amt, :st, :idk)
            ON CONFLICT (client_idk) DO UPDATE
              SET updated_at = now()
            RETURNING id, user_id, amount, status, client_idk,
                      hold_done, refund_done, payout_ref, created_at, updated_at
            """
        )
        r = await db.execute(
            sql,
            {
                "uid": int(user_id),
                "amt": amt,
                "st": WITHDRAW_REQ_REQUESTED,
                "idk": str(client_idk),
            },
        )
        row = r.fetchone()
        await db.commit()
    except Exception as e:
        await db.rollback()
        logger.error("request_withdraw insert failed (user=%s, client_idk=%s): %s", user_id, client_idk, e)
        raise

    dto = _row_to_dto(row)

    # Если уже был выполнен холд (повтор по client_idk) — вернуть существующую заявку
    if dto.hold_done:
        return dto

    # 2) Выполняем холд: списать у пользователя EFHC в Банк (Только main_balance)
    try:
        await debit_user_to_bank(
            db,
            user_id=dto.user_id,
            amount_efhc=dto.amount,
            reason="withdraw_hold",
            idempotency_key=f"{client_idk}:hold",
        )
    except Exception as e:
        logger.error("withdraw_hold failed (user=%s, req_id=%s): %s", dto.user_id, dto.id, e)
        # Холд не прошёл — заявку не удаляем; пользователь может пополнить и повторить.
        # Статус остаётся REQUESTED, hold_done = FALSE. ensure_consistency() попытается
        # дохолдировать автоматически.
        raise RuntimeError("Недостаточно средств или временная ошибка холда.") from e

    # 3) Отметим hold_done = TRUE
    try:
        u = text(
            f"""
            UPDATE {SCHEMA}.withdraw_requests
               SET hold_done = TRUE,
                   updated_at = now()
             WHERE id = :rid
            RETURNING id, user_id, amount, status, client_idk,
                      hold_done, refund_done, payout_ref, created_at, updated_at
            """
        )
        r2 = await db.execute(u, {"rid": int(dto.id)})
        row2 = r2.fetchone()
        await db.commit()
        return _row_to_dto(row2)
    except Exception as e:
        await db.rollback()
        logger.warning(
            "withdraw_hold flag update failed (non-fatal, user=%s, req_id=%s): %s",
            dto.user_id,
            dto.id,
            e,
        )
        # Даже если отметка hold_done не проставилась — деньги уже в Банке.
        # ensure_consistency() сможет восстановить флаг.
        return dto


# =============================================================================
# Отмена пользователем (если не PAID) с рефандом, если холд был
# =============================================================================

async def cancel_withdraw(
    db: AsyncSession,
    *,
    request_id: int,
    user_id: int,
    client_idk: str,
) -> WithdrawRequestDTO:
    """
    Отмена заявки пользователем.

    Что делает:
      • Помечает заявку как CANCELED (если не PAID) и выполняет рефанд из Банка,
        если был холд (hold_done=True и refund_done=False).

    Идемпотентность:
      • Рефанд идемпотентен по ключу f"{client_idk}:refund".
    """
    q = text(
        f"""
        SELECT id, user_id, amount, status, client_idk,
               hold_done, refund_done, payout_ref, created_at, updated_at
          FROM {SCHEMA}.withdraw_requests
         WHERE id = :rid AND user_id = :uid
         LIMIT 1
        """
    )
    r = await db.execute(q, {"rid": int(request_id), "uid": int(user_id)})
    row = r.fetchone()
    if not row:
        raise ValueError("withdraw request not found")

    dto = _row_to_dto(row)

    # Если уже PAID — отменять нельзя, просто отдаем состояние
    if dto.status == WITHDRAW_REQ_PAID:
        return dto

    # Если холд был и рефанда ещё нет — вернём средства пользователю
    if dto.hold_done and not dto.refund_done:
        try:
            await credit_user_from_bank(
                db,
                user_id=dto.user_id,
                amount_efhc=dto.amount,
                reason="withdraw_refund",
                idempotency_key=f"{dto.client_idk}:refund",
            )
        except Exception as e:
            logger.error("withdraw_refund failed (user=%s, req_id=%s): %s", dto.user_id, dto.id, e)
            raise RuntimeError("Рефанд временно недоступен, повторите позже.") from e

    # Проставим статус/флаг refund_done
    try:
        u = text(
            f"""
            UPDATE {SCHEMA}.withdraw_requests
               SET status      = :st,
                   refund_done = (refund_done OR :rfd),
                   updated_at  = now()
             WHERE id = :rid
            RETURNING id, user_id, amount, status, client_idk,
                      hold_done, refund_done, payout_ref, created_at, updated_at
            """
        )
        r2 = await db.execute(
            u,
            {
                "st": WITHDRAW_REQ_CANCELED,
                "rfd": True if dto.hold_done else False,
                "rid": int(dto.id),
            },
        )
        row2 = r2.fetchone()
        await db.commit()
        return _row_to_dto(row2)
    except Exception as e:
        await db.rollback()
        logger.error("cancel_withdraw update failed (user=%s, req_id=%s): %s", dto.user_id, dto.id, e)
        raise


# =============================================================================
# Действия админа: approve / reject / mark_paid
# =============================================================================

async def admin_approve_withdraw(
    db: AsyncSession,
    *,
    request_id: int,
    admin_user_id: int,
) -> WithdrawRequestDTO:
    """
    Админ: пометить заявку как APPROVED.

    Денежных движений нет — холд был выполнен при REQUESTED и остаётся в Банке.
    """
    u = text(
        f"""
        UPDATE {SCHEMA}.withdraw_requests
           SET status     = :st,
               updated_at = now()
         WHERE id = :rid
        RETURNING id, user_id, amount, status, client_idk,
                  hold_done, refund_done, payout_ref, created_at, updated_at
        """
    )
    r = await db.execute(u, {"st": WITHDRAW_REQ_APPROVED, "rid": int(request_id)})
    row = r.fetchone()
    if not row:
        await db.rollback()
        raise ValueError("withdraw request not found")
    await db.commit()
    return _row_to_dto(row)


async def admin_reject_withdraw(
    db: AsyncSession,
    *,
    request_id: int,
    admin_user_id: int,
) -> WithdrawRequestDTO:
    """
    Админ: отклонить заявку.

    Что делает:
      • Помечает заявку как REJECTED.
      • Если холд был и рефанда ещё нет — возвращает EFHC пользователю (рефанд).
    """
    q = text(
        f"""
        SELECT id, user_id, amount, status, client_idk,
               hold_done, refund_done, payout_ref, created_at, updated_at
          FROM {SCHEMA}.withdraw_requests
         WHERE id = :rid
         LIMIT 1
        """
    )
    r = await db.execute(q, {"rid": int(request_id)})
    row = r.fetchone()
    if not row:
        raise ValueError("withdraw request not found")

    dto = _row_to_dto(row)

    # Рефанд если нужно
    if dto.hold_done and not dto.refund_done:
        try:
            await credit_user_from_bank(
                db,
                user_id=dto.user_id,
                amount_efhc=dto.amount,
                reason="withdraw_reject_refund",
                idempotency_key=f"{dto.client_idk}:refund",
            )
        except Exception as e:
            logger.error("reject withdraw_refund failed (user=%s, req_id=%s): %s", dto.user_id, dto.id, e)
            raise RuntimeError("Рефанд временно недоступен, повторите позже.") from e

    # Обновление статуса
    u = text(
        f"""
        UPDATE {SCHEMA}.withdraw_requests
           SET status      = :st,
               refund_done = (refund_done OR :rfd),
               updated_at  = now()
         WHERE id = :rid
        RETURNING id, user_id, amount, status, client_idk,
                  hold_done, refund_done, payout_ref, created_at, updated_at
        """
    )
    r2 = await db.execute(
        u,
        {
            "st": WITHDRAW_REQ_REJECTED,
            "rfd": True if dto.hold_done else False,
            "rid": int(request_id),
        },
    )
    row2 = r2.fetchone()
    await db.commit()
    return _row_to_dto(row2)


async def admin_mark_paid(
    db: AsyncSession,
    *,
    request_id: int,
    admin_user_id: int,
    payout_ref: Optional[str] = None,
) -> WithdrawRequestDTO:
    """
    Админ: пометить заявку как PAID.

    Денежных движений здесь нет:
      • Холд был сделан при REQUESTED и остаётся в Банке.
      • Внешнее списание (биржа, DEX и т.п.) выполняется вручную/интеграцией.
    """
    u = text(
        f"""
        UPDATE {SCHEMA}.withdraw_requests
           SET status     = :st,
               payout_ref = COALESCE(:pref, payout_ref),
               updated_at = now()
         WHERE id = :rid
        RETURNING id, user_id, amount, status, client_idk,
                  hold_done, refund_done, payout_ref, created_at, updated_at
        """
    )
    r = await db.execute(
        u,
        {
            "st": WITHDRAW_REQ_PAID,
            "pref": (payout_ref or None),
            "rid": int(request_id),
        },
    )
    row = r.fetchone()
    if not row:
        await db.rollback()
        raise ValueError("withdraw request not found")
    await db.commit()
    return _row_to_dto(row)


# =============================================================================
# Витрины/списки (курсорно, без OFFSET) — для роутов
# =============================================================================

async def list_user_withdraws(
    db: AsyncSession,
    *,
    user_id: int,
    limit: int = 100,
    cursor: Optional[str] = None,
    status_filter: Optional[str] = None,
) -> WithdrawPageDTO:
    """
    Возвращает страницу заявок пользователя (упорядочено по created_at, id).

    Курсор:
      • cursor = json.dumps({"ts": created_at_iso, "id": last_id})
    """
    if limit < 1:
        limit = 1
    if limit > 200:
        limit = 200

    c_ts, c_id = _decode_cursor(cursor)

    base = f"""
        SELECT id, user_id, amount, status, client_idk,
               hold_done, refund_done, payout_ref, created_at, updated_at
          FROM {SCHEMA}.withdraw_requests
         WHERE user_id = :uid
    """
    params: Dict[str, Any] = {"uid": int(user_id), "lim": int(limit)}

    if status_filter:
        base += " AND status = :sf"
        params["sf"] = str(status_filter)

    if c_ts is not None and c_id is not None:
        base += " AND ((created_at > :c_ts) OR (created_at = :c_ts AND id > :c_id))"
        params["c_ts"] = c_ts.astimezone(timezone.utc)
        params["c_id"] = int(c_id)

    base += " ORDER BY created_at ASC, id ASC LIMIT :lim"

    r = await db.execute(text(base), params)
    rows = r.fetchall()

    items = [_row_to_dto(row) for row in rows]
    next_cursor = None
    if rows:
        last_ts: datetime = rows[-1][8].astimezone(timezone.utc)  # created_at
        last_id: int = int(rows[-1][0])                           # id
        next_cursor = _encode_cursor(last_ts, last_id)

    return WithdrawPageDTO(items=items, next_cursor=next_cursor)


# =============================================================================
# ИИ-самовосстановление / аудит консистентности
#
#   Находит «висячие» заявки и доводит их до правильного состояния:
#     • REQUESTED без hold_done → пытаемся выполнить холд.
#     • REJECTED/CANCELED с hold_done, но без refund_done → рефанд в пользователя.
#
#   Пакетная обработка ограничена по времени и количеству.
# =============================================================================

async def ensure_consistency(
    db: AsyncSession,
    *,
    scan_minutes: int = 240,   # смотреть хвост за последние 4 часа
    batch_limit: int = 200,
) -> Dict[str, int]:
    """
    Авто-ремонт консистентности заявок на вывод.

    Возвращает словарь:
      • {"auto_fixed_hold": N, "auto_fixed_refund": M}
    """
    fixed_hold = 0
    fixed_refund = 0

    # 1) Догон холда (REQUESTED & hold_done = FALSE)
    try:
        q1 = text(
            f"""
            SELECT id, user_id, amount, status, client_idk,
                   hold_done, refund_done, payout_ref, created_at, updated_at
              FROM {SCHEMA}.withdraw_requests
             WHERE status = :st
               AND hold_done = FALSE
               AND created_at >= (now() - INTERVAL :mins)
             ORDER BY created_at ASC
             LIMIT :lim
            """
        )
        r1 = await db.execute(
            q1,
            {
                "st": WITHDRAW_REQ_REQUESTED,
                "mins": f"{int(scan_minutes)} minutes",
                "lim": int(batch_limit),
            },
        )
        rows1 = r1.fetchall()
    except Exception as e:
        logger.warning("ensure_consistency fetch REQUESTED failed: %s", e)
        rows1 = []

    for row in rows1:
        dto = _row_to_dto(row)
        try:
            await debit_user_to_bank(
                db,
                user_id=dto.user_id,
                amount_efhc=dto.amount,
                reason="withdraw_hold_autofix",
                idempotency_key=f"{dto.client_idk}:hold",
            )
            u = text(
                f"""
                UPDATE {SCHEMA}.withdraw_requests
                   SET hold_done = TRUE,
                       updated_at = now()
                 WHERE id = :rid
                """
            )
            await db.execute(u, {"rid": int(dto.id)})
            await db.commit()
            fixed_hold += 1
        except Exception as e:
            await db.rollback()
            logger.warning("ensure_consistency hold autofix failed (req_id=%s): %s", dto.id, e)
            continue

    # 2) Догон рефанда (REJECTED/CANCELED & hold_done=TRUE & refund_done=FALSE)
    try:
        q2 = text(
            f"""
            SELECT id, user_id, amount, status, client_idk,
                   hold_done, refund_done, payout_ref, created_at, updated_at
              FROM {SCHEMA}.withdraw_requests
             WHERE (status = :st1 OR status = :st2)
               AND hold_done = TRUE
               AND refund_done = FALSE
               AND updated_at >= (now() - INTERVAL :mins)
             ORDER BY updated_at ASC
             LIMIT :lim
            """
        )
        r2 = await db.execute(
            q2,
            {
                "st1": WITHDRAW_REQ_REJECTED,
                "st2": WITHDRAW_REQ_CANCELED,
                "mins": f"{int(scan_minutes)} minutes",
                "lim": int(batch_limit),
            },
        )
        rows2 = r2.fetchall()
    except Exception as e:
        logger.warning("ensure_consistency fetch REFUND failed: %s", e)
        rows2 = []

    for row in rows2:
        dto = _row_to_dto(row)
        try:
            await credit_user_from_bank(
                db,
                user_id=dto.user_id,
                amount_efhc=dto.amount,
                reason="withdraw_refund_autofix",
                idempotency_key=f"{dto.client_idk}:refund",
            )
            u = text(
                f"""
                UPDATE {SCHEMA}.withdraw_requests
                   SET refund_done = TRUE,
                       updated_at  = now()
                 WHERE id = :rid
                """
            )
            await db.execute(u, {"rid": int(dto.id)})
            await db.commit()
            fixed_refund += 1
        except Exception as e:
            await db.rollback()
            logger.warning("ensure_consistency refund autofix failed (req_id=%s): %s", dto.id, e)
            continue

    return {"auto_fixed_hold": fixed_hold, "auto_fixed_refund": fixed_refund}


# =============================================================================
# Пояснения «для чайника»:
#  • Почему холд сразу при создании?
#    Чтобы пользователь не смог параллельно потратить те же EFHC на панели или
#    другие операции: деньги немедленно переводятся с его баланса в Банк.
#
#  • Что если холд упал?
#    Заявка остаётся REQUESTED с hold_done = FALSE. Пользователь может пополнить
#    счёт (например, обменять kWh→EFHC) и повторить. Также планировщик вызывает
#    ensure_consistency(), который пытается повторить холд с тем же idempotency_key.
#
#  • Когда деньги реально «уходят» из системы?
#    В этом модуле — никогда. Мы только переводим EFHC пользователя в Банк
#    (холд) и при необходимости возвращаем обратно (рефанд). Внешнее списание
#    (вывод на биржу/DEX и т.д.) — ручной процесс администратора/интеграции.
#
#  • Почему бонусы нельзя вывести?
#    Канон: бонусные EFHC невыплатные. Они используются только в рамках игры/
#    промо и при расходовании их номинал возвращается в Банк.
# =============================================================================
