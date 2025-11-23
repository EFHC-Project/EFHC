"""TON watcher service with идемпотентностью и самовосстановлением.

Каркас обходит логи входящих переводов, парсит MEMO, начисляет EFHC через
централизованный банковский сервис и помечает статусы с ретраями каждые
10 минут. Все исключения локализуются внутри цикла и не валят планировщик.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.logging_core import get_logger
from ..core.utils_core import utc_now
from ..integrations.ton_api import ParsedMemo, parse_memo
from ..models import TonInboxLog, User
from .transactions_service import TransactionsService

logger = get_logger(__name__)


class WatcherService:
    """Обработчик входящих TON-платежей с идемпотентностью."""

    def __init__(self, session: AsyncSession):
        self.session = session
        self.transactions = TransactionsService(session)

    async def _credit_user(self, parsed: ParsedMemo, log: TonInboxLog) -> None:
        user = await self.session.scalar(select(User).where(User.telegram_id == parsed.telegram_id))
        if user is None:
            logger.warning("user not found for top-up", extra={"telegram_id": parsed.telegram_id})
            log.status = "error_missing_user"
            log.next_retry_at = utc_now() + timedelta(minutes=10)
            log.retries_count += 1
            return

        amount = parsed.quantity if parsed.quantity > 0 else log.amount
        if amount <= Decimal("0"):
            logger.warning("zero-amount top-up skipped", extra={"tx_hash": log.tx_hash})
            log.status = "error_zero_amount"
            log.next_retry_at = utc_now() + timedelta(minutes=10)
            log.retries_count += 1
            return

        idempotency_key = f"ton:{log.tx_hash}"
        transfer = await self.transactions.credit_user(
            user=user, amount=amount, idempotency_key=idempotency_key, tx_hash=log.tx_hash
        )
        log.status = "credited"
        log.next_retry_at = None
        logger.info(
            "ton credit processed",
            extra={"tx_hash": log.tx_hash, "user_id": user.id, "amount": str(transfer.amount)},
        )

    async def tick(self) -> None:
        """Сделать один проход по неподтверждённым логам."""

        pending = await self.session.scalars(
            select(TonInboxLog).where(
                TonInboxLog.status.in_(
                    ["received", "parsed", "error_parse", "error_missing_user", "error_zero_amount"]
                ),
                (TonInboxLog.next_retry_at.is_(None)) | (TonInboxLog.next_retry_at <= utc_now()),
            )
        )
        logs = list(pending)
        logger.info("watcher tick", extra={"batch": len(logs)})
        for log in logs:
            try:
                parsed = parse_memo(log.memo)
                log.status = "parsed"
                await self._credit_user(parsed, log)
            except ValueError as exc:  # детерминированная ошибка парсера
                log.status = "error_parse"
                log.next_retry_at = utc_now() + timedelta(minutes=10)
                log.retries_count += 1
                logger.warning("memo parse failed", extra={"tx_hash": log.tx_hash, "memo": log.memo, "error": str(exc)})
            except Exception as exc:  # noqa: BLE001 - фиксируем любой сбой, не прерывая цикл
                log.status = "error_unexpected"
                log.next_retry_at = utc_now() + timedelta(minutes=10)
                log.retries_count += 1
                logger.exception("ton watcher crash isolated", extra={"tx_hash": log.tx_hash, "error": str(exc)})
        await self.session.commit()
        logger.info("watcher tick executed", extra={"processed": len(logs)})
