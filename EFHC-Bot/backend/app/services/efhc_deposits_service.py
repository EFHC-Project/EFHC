# -*- coding: utf-8 -*-
# backend/app/services/efhc_deposits_service.py
# =============================================================================
# EFHC Bot — приём внешних депозитов EFHC с блокчейна во внутренний банк бота
# -----------------------------------------------------------------------------
# Назначение:
#   • После того как вотчер увидел входящую on-chain транзакцию EFHC
#     на проектный внешний кошелёк, этот сервис:
#       1) Однозначно привязывает её к пользователю бота.
#       2) Начисляет пользователю EFHC из центрального "банка EFHC"
#          по курсу 1:1 (внутренняя транзакция).
#       3) Гарантирует идемпотентность по tx_hash (никаких дублей).
#
# Канон/инварианты:
#   • Источник внутренних EFHC — ТОЛЬКО банк EFHC
#     (transactions_service.credit_user_from_bank).
#   • Пользователь НИКОГДА не уходит в минус. Банк МОЖЕТ (операции не блокируем).
#   • Курс фиксированный: 1 EFHC on-chain = 1 EFHC внутри бота.
#   • Идемпотентность:
#       idempotency_key = "deposit:efhc:<tx_hash>"
#     Повторная обработка той же транзакции НЕ создаёт повторных начислений.
#   • Привязка к пользователю:
#       1) Если есть корректный MEMO → telegram_id.
#       2) Если MEMO нет/невалиден → по адресу отправителя через таблицу
#          привязанных кошельков (wallet_models.UserWallet).
#   • Если пользователя определить нельзя — депозит НЕ зачисляется, возвращаем
#     error=user_not_resolved (для ручной обработки в админке).
#
# Контекст использования:
#   • Внешний TON-кошелёк проекта принимает EFHC от пользователя.
#   • Старт этой транзакции инициируется нажатием кнопки в боте
#     ("Пополнить EFHC"), но сервис депозитов этого не требует — ему важен факт
#     on-chain транзакции.
#   • Начальный баланс банка EFHC (например, 5_000_000 EFHC) создаётся миграцией
#     или отдельным скриптом — здесь мы только уменьшаем банк/увеличиваем
#     баланс пользователя через banking-сервис.
# =============================================================================

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Optional

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config_core import get_settings
from backend.app.core.logging_core import get_logger
from backend.app.deps import d8  # Decimal с 8 знаками, округление вниз

from backend.app.models.user_models import User
from backend.app.models.wallet_models import UserWallet
from backend.app.services import transactions_service as tx

logger = get_logger(__name__)
settings = get_settings()
SCHEMA_CORE = getattr(settings, "DB_SCHEMA_CORE", "efhc_core") or "efhc_core"


# -----------------------------------------------------------------------------
# Ошибки и утилиты
# -----------------------------------------------------------------------------

class DepositError(Exception):
    """Ошибка сервиса депозитов EFHC (для UI/логов, не про деньги)."""


def _as_decimal(value: Any) -> Decimal:
    """Безопасно привести к Decimal."""
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise DepositError("Некорректное числовое значение amount для депозита EFHC") from exc


@dataclass
class ResolvedUser:
    user_id: int
    telegram_id: int


# -----------------------------------------------------------------------------
# Привязка транзакции к пользователю
# -----------------------------------------------------------------------------

async def _resolve_user_by_memo(db: AsyncSession, memo: Optional[str]) -> Optional[ResolvedUser]:
    """
    Приоритетный способ: MEMO = telegram_id пользователя.
    MEMO не обязателен, но если он есть и валиден — используем его.
    """
    if not memo:
        return None
    memo = str(memo).strip()
    if not memo.isdigit():
        return None

    tg_id = int(memo)
    res = await db.execute(
        select(User.id, User.telegram_id).where(User.telegram_id == tg_id).limit(1)
    )
    row = res.fetchone()
    if not row:
        return None
    return ResolvedUser(user_id=int(row[0]), telegram_id=int(row[1] or 0))


async def _resolve_user_by_wallet(db: AsyncSession, from_address: Optional[str]) -> Optional[ResolvedUser]:
    """
    Резервный способ: по адресу отправителя через таблицу привязанных кошельков.
    Логика:
      1) Нормализуем адрес (strip).
      2) Ищем UserWallet.wallet_address == addr_norm.
      3) По найденному telegram_id находим User.
    """
    if not from_address:
        return None
    addr_norm = str(from_address).strip()
    if not addr_norm:
        return None

    res = await db.execute(
        select(UserWallet.telegram_id)
        .where(UserWallet.wallet_address == addr_norm)
        .order_by(UserWallet.id.asc())
        .limit(1)
    )
    row = res.fetchone()
    if not row:
        return None
    tg_id = int(row[0])

    res_u = await db.execute(
        select(User.id, User.telegram_id).where(User.telegram_id == tg_id).limit(1)
    )
    urow = res_u.fetchone()
    if not urow:
        return None
    return ResolvedUser(user_id=int(urow[0]), telegram_id=int(urow[1] or 0))


async def _resolve_deposit_user(
    db: AsyncSession,
    *,
    memo: Optional[str],
    from_address: Optional[str],
) -> Optional[ResolvedUser]:
    """
    Каноническая последовательность:
      1) Сначала пробуем MEMO (telegram_id).
      2) Если не сработало — пробуем по адресу отправителя.
    """
    user = await _resolve_user_by_memo(db, memo)
    if user:
        return user
    return await _resolve_user_by_wallet(db, from_address)


# -----------------------------------------------------------------------------
# Основная функция приёма депозита EFHC
# -----------------------------------------------------------------------------

async def process_efhc_deposit(
    db: AsyncSession,
    *,
    tx_hash: str,
    amount: Any,
    memo: Optional[str],
    from_address: Optional[str],
    to_address: Optional[str] = None,
    raw_tx: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Обработка одной on-chain транзакции EFHC на проектный внешний кошелёк.

    Вход:
      • tx_hash      — хэш транзакции (строка, уникальный в сети).
      • amount       — количество EFHC on-chain (Decimal-совместимое).
      • memo         — комментарий (ожидаем telegram_id; не обязателен).
      • from_address — кошелёк отправителя (пользователь).
      • to_address   — кошелёк проекта (для логов/контроля).
      • raw_tx       — сырые данные транзакции (dict, опционально для аудита).

    Логика:
      1) amount > 0, приводим к Decimal с 8 знаками (d8).
      2) Определяем пользователя:
           a) MEMO → telegram_id;
           b) если MEMO нет/невалиден → таблица UserWallet по from_address.
      3) Если пользователя не нашли → error=user_not_resolved, без начисления.
      4) Начисляем пользователю EFHC из банка по курсу 1:1 через
         credit_user_from_bank(...) с idempotency_key="deposit:efhc:<tx_hash>".
      5) Читаем свежие балансы main/bonus для ответа.

    Важно:
      • Эта функция НИКОГДА не трогает on-chain балансы напрямую.
        Вся логика — внутренний банк EFHC.
      • Коммита/rollback внутри нет — оборачивайте вызов в async with session.begin().
    """
    if not tx_hash or not str(tx_hash).strip():
        raise DepositError("tx_hash обязателен для идемпотентности депозита EFHC")

    # 1) Сумма
    dec_amount = d8(_as_decimal(amount))
    if dec_amount <= Decimal("0"):
        raise DepositError("Сумма депозита EFHC должна быть больше 0")

    # 2) Определяем пользователя
    resolved = await _resolve_deposit_user(db, memo=memo, from_address=from_address)
    if not resolved:
        logger.warning(
            "EFHC deposit: не удалось привязать транзакцию к пользователю "
            "(tx_hash=%s, amount=%s, memo=%r, from=%r)",
            tx_hash,
            dec_amount,
            memo,
            from_address,
        )
        return {
            "ok": False,
            "error": "user_not_resolved",
            "detail": "Пользователь не найден ни по MEMO, ни по адресу кошелька.",
            "tx_hash": tx_hash,
            "amount": str(dec_amount),
            "memo": memo,
            "from_address": from_address,
            "to_address": to_address,
        }

    user_id = resolved.user_id
    telegram_id = resolved.telegram_id

    # 3) Начисление из банка EFHC 1:1
    idem_key = f"deposit:efhc:{tx_hash}"
    meta: Dict[str, Any] = {
        "domain": "efhc_external_deposit",
        "tx_hash": tx_hash,
        "from_address": from_address,
        "to_address": to_address,
        "memo": memo,
        "telegram_id": telegram_id,
    }
    if raw_tx is not None:
        meta["raw_tx"] = raw_tx

    try:
        await tx.credit_user_from_bank(
            db=db,
            user_id=user_id,
            amount=dec_amount,
            reason="efhc_external_deposit",
            idempotency_key=idem_key,
            meta=meta,
        )
    except Exception as exc:
        logger.exception(
            "EFHC deposit: ошибка при credit_user_from_bank (tx_hash=%s, user_id=%s): %s",
            tx_hash,
            user_id,
            exc,
        )
        raise DepositError(f"Не удалось зачислить EFHC пользователю: {type(exc).__name__}: {exc}") from exc

    # 4) Читаем свежие балансы пользователя (для логов/ответа)
    row = await db.execute(
        text(
            f"""
            SELECT COALESCE(main_balance,0)  AS main_balance,
                   COALESCE(bonus_balance,0) AS bonus_balance
              FROM {SCHEMA_CORE}.users
             WHERE id = :uid
            """
        ),
        {"uid": int(user_id)},
    )
    r = row.fetchone()
    main_balance = d8(r.main_balance) if r else Decimal("0")   # type: ignore[attr-defined]
    bonus_balance = d8(r.bonus_balance) if r else Decimal("0") # type: ignore[attr-defined]

    logger.info(
        "EFHC deposit processed: user_id=%s tg=%s amount=%s tx_hash=%s",
        user_id,
        telegram_id,
        dec_amount,
        tx_hash,
    )

    return {
        "ok": True,
        "tx_hash": tx_hash,
        "amount": str(dec_amount),
        "user_id": user_id,
        "telegram_id": telegram_id,
        "from_address": from_address,
        "to_address": to_address,
        "memo": memo,
        "idempotency_key": idem_key,
        "new_main_balance": str(main_balance),
        "new_bonus_balance": str(bonus_balance),
    }


# =============================================================================
# Пример интеграции с вотчером (для разработчика):
# -----------------------------------------------------------------------------
# async def handle_new_efhc_transfer(tx):
#     async with async_session_maker() as db:
#         async with db.begin():
#             result = await process_efhc_deposit(
#                 db,
#                 tx_hash=tx.hash,
#                 amount=tx.amount,        # Decimal или строка
#                 memo=tx.comment,         # может быть None
#                 from_address=tx.from_addr,
#                 to_address=tx.to_addr,
#                 raw_tx=tx.to_dict(),     # опционально
#             )
#     # result["ok"] / result["error"] можно использовать для уведомлений/логов.
#
# Стартовый баланс банка EFHC (например, 5_000_000 EFHC) должен быть задан
# миграцией/скриптом в таблице банка (см. transactions_service и описание Банка).
# =============================================================================

__all__ = [
    "DepositError",
    "process_efhc_deposit",
]
