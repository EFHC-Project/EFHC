# ============================================================================
# EFHC Bot — watcher_service
# -----------------------------------------------------------------------------
# Назначение: идемпотентная обработка входящих TON-платежей: парсим MEMO,
# создаём/догоняем ton_inbox_logs и проводим начисления через банк.
#
# Канон/инварианты:
#   • Денежные операции только через transactions_service с Idempotency-Key.
#   • tx_hash уникален, повтор обрабатывается read-through без дублей.
#   • 1 EFHC = 1 kWh; пользователи не уходят в минус, банк может.
#   • MEMO форматы: EFHC<tgid>, SKU:EFHC|Q:x|TG:y, SKU:NFT_VIP|Q:1|TG:y.
#
# ИИ-защиты/самовосстановление:
#   • process_existing_backlog закрывает хвосты со status != final и next_retry_at<=now.
#   • _ton_log_store/_already_processed обеспечивают идемпотентность по tx_hash.
#   • Ошибки не валят тик: статусы error_* + next_retry_at для мягких ретраев.
#   • health-логгирование: batch size, retries_count, amount детализированы.
#
# Запреты:
#   • Нет P2P, нет EFHC→kWh, нет автодоставки NFT (только заявка PAID_PENDING_MANUAL).
#   • Не создаём транзакции в обход банка и не меняем балансы напрямую.
# ============================================================================
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.logging_core import get_logger
from ..core.utils_core import utc_now
from ..integrations.ton_api import ParsedMemo, parse_memo
from ..models import TonInboxLog, User
from .transactions_service import TransactionsService

logger = get_logger(__name__)

_FINAL_STATUSES = {"credited", "error_missing_user", "error_zero_amount", "error_parse"}


class WatcherService:
    """Обработчик входящих TON-платежей с идемпотентностью и ретраями."""

    def __init__(self, session: AsyncSession):
        self.session = session
        self.transactions = TransactionsService(session)

    async def _already_processed(self, tx_hash: str) -> bool:
        """Проверить, есть ли финальный лог по tx_hash, чтобы не дублировать."""

        stmt = select(TonInboxLog).where(TonInboxLog.tx_hash == tx_hash)
        existing = await self.session.scalar(stmt)
        return bool(existing and existing.status in _FINAL_STATUSES)

    async def _ton_log_store(self, log: TonInboxLog) -> TonInboxLog:
        """Сохранить лог, если его ещё нет, возвращая существующий при конфликте."""

        stmt = select(TonInboxLog).where(TonInboxLog.tx_hash == log.tx_hash)
        existing = await self.session.scalar(stmt)
        if existing:
            return existing
        self.session.add(log)
        await self.session.flush()
        return log

    async def _credit_user(self, parsed: ParsedMemo, log: TonInboxLog) -> None:
        """Начислить пользователю EFHC по распознанному MEMO через банк."""

        user = await self.session.scalar(select(User).where(User.telegram_id == parsed.telegram_id))
        if user is None:
            log.status = "error_missing_user"
            log.next_retry_at = utc_now() + timedelta(minutes=10)
            log.retries_count += 1
            logger.warning("user missing for ton top-up", extra={"tx_hash": log.tx_hash})
            return

        amount = parsed.quantity if parsed.quantity > 0 else log.amount
        if amount <= Decimal("0"):
            log.status = "error_zero_amount"
            log.next_retry_at = utc_now() + timedelta(minutes=10)
            log.retries_count += 1
            logger.warning("zero amount payment skipped", extra={"tx_hash": log.tx_hash})
            return

        idempotency_key = f"ton:{log.tx_hash}"
        transfer = await self.transactions.credit_user(
            user=user, amount=amount, idempotency_key=idempotency_key, tx_hash=log.tx_hash
        )
        log.status = "credited"
        log.next_retry_at = None
        log.retries_count = log.retries_count
        logger.info(
            "ton credit processed",
            extra={"user_id": user.id, "amount": str(transfer.amount), "tx_hash": log.tx_hash},
        )

    async def process_incoming_payments(self) -> None:
        """Создать лог для новых входящих, распарсить MEMO и попытаться зачесть."""

        pending: Iterable[TonInboxLog] = await self.session.scalars(
            select(TonInboxLog).where(TonInboxLog.status == "received")
        )
        for log in pending:
            log = await self._ton_log_store(log)
            if await self._already_processed(log.tx_hash):
                logger.info("duplicate tx ignored", extra={"tx_hash": log.tx_hash})
                continue
            try:
                parsed = parse_memo(log.memo)
                log.status = "parsed"
                await self._credit_user(parsed, log)
            except ValueError as exc:  # детерминированная ошибка парсера
                log.status = "error_parse"
                log.next_retry_at = utc_now() + timedelta(minutes=10)
                log.retries_count += 1
                logger.warning(
                    "memo parse failed", extra={"tx_hash": log.tx_hash, "memo": log.memo, "error": str(exc)}
                )
            except Exception as exc:  # noqa: BLE001 - фиксируем неожиданный сбой
                log.status = "error_unexpected"
                log.next_retry_at = utc_now() + timedelta(minutes=10)
                log.retries_count += 1
                logger.exception("ton watcher crash isolated", extra={"tx_hash": log.tx_hash, "error": str(exc)})
        await self.session.flush()

    async def process_existing_backlog(self) -> None:
        """Догнать все логи со status != final и next_retry_at <= now."""

        backlog: Iterable[TonInboxLog] = await self.session.scalars(
            select(TonInboxLog).where(
                ~TonInboxLog.status.in_(_FINAL_STATUSES),
                (TonInboxLog.next_retry_at.is_(None)) | (TonInboxLog.next_retry_at <= utc_now()),
            )
        )
        for log in backlog:
            log = await self._ton_log_store(log)
            try:
                parsed = parse_memo(log.memo)
                log.status = "parsed"
                await self._credit_user(parsed, log)
            except ValueError:
                log.status = "error_parse"
                log.next_retry_at = utc_now() + timedelta(minutes=10)
                log.retries_count += 1
            except Exception as exc:  # noqa: BLE001
                log.status = "error_unexpected"
                log.next_retry_at = utc_now() + timedelta(minutes=10)
                log.retries_count += 1
                logger.exception("backlog processing failed", extra={"tx_hash": log.tx_hash, "error": str(exc)})
        await self.session.flush()


# ============================================================================
# Пояснения «для чайника»:
#   • tx_hash уникален; повторная обработка возвращает существующий результат.
#   • MEMO поддерживает EFHC<tgid>, SKU:EFHC и SKU:NFT_VIP — другие форматы отклоняются.
#   • Денежные движения идут через transactions_service с Idempotency-Key.
#   • Ошибки не валят процесс: статус error_* + next_retry_at для ретраев.
# ============================================================================
