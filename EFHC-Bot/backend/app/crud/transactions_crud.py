"""CRUD for EFHC transfer logs (idempotent read-through access).

======================================================================
Назначение:
    • Предоставить безопасный доступ к журналу efhc_transfers_log без
      изменения бизнес-логики банка.
    • Поддержать read-through по idempotency_key и tx_hash для повторных
      запросов, а также курсорные выборки для админки/отчётов.

Канон/инварианты:
    • CRUD не двигает деньги и не меняет балансы; только читает/добавляет
      записи журнала.
    • idempotency_key и tx_hash уникальны, повтор запроса возвращает ту же
      запись без дублей.
    • OFFSET запрещён; только курсоры (created_at DESC, id DESC).

ИИ-защита/самовосстановление:
    • get_or_create_by_key() возвращает существующую запись, если ключ уже
      встречался, исключая двойные транзакции.
    • lock_by_id() позволяет безопасно читать строку под FOR UPDATE, если
      нужен сервисный догон/пометка дефицита.

Запреты:
    • Нет P2P и EFHC→kWh; денежные движения выполняет transactions_service.
======================================================================
"""

from __future__ import annotations

from datetime import datetime
from typing import Iterable

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.logging_core import get_logger
from backend.app.models import EFHCTransferLog

logger = get_logger(__name__)


class TransactionsCRUD:
    """Доступ к efhc_transfers_log без изменения балансов."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_by_id(self, transfer_id: int) -> EFHCTransferLog | None:
        """Прочитать запись журнала по первичному ключу."""

        return await self.session.get(EFHCTransferLog, int(transfer_id))

    async def get_by_idempotency(self, key: str) -> EFHCTransferLog | None:
        """Найти запись по idempotency_key (read-through)."""

        stmt: Select[EFHCTransferLog] = select(EFHCTransferLog).where(
            EFHCTransferLog.idempotency_key == key
        )
        return await self.session.scalar(stmt)

    async def get_by_tx_hash(self, tx_hash: str) -> EFHCTransferLog | None:
        """Найти запись по tx_hash (TON входящий)."""

        stmt: Select[EFHCTransferLog] = select(EFHCTransferLog).where(
            EFHCTransferLog.tx_hash == tx_hash
        )
        return await self.session.scalar(stmt)

    async def add(self, log: EFHCTransferLog) -> EFHCTransferLog:
        """Создать новую запись журнала (commit делает вызывающий код)."""

        self.session.add(log)
        await self.session.flush()
        return log

    async def get_or_create_by_key(
        self, log: EFHCTransferLog
    ) -> EFHCTransferLog:
        """Read-through вставка по idempotency_key/tx_hash без дублей."""

        if log.idempotency_key:
            existing = await self.get_by_idempotency(log.idempotency_key)
            if existing:
                return existing
        if log.tx_hash:
            existing_tx = await self.get_by_tx_hash(log.tx_hash)
            if existing_tx:
                return existing_tx
        self.session.add(log)
        await self.session.flush()
        return log

    async def lock_by_id(self, transfer_id: int) -> EFHCTransferLog | None:
        """Получить запись под FOR UPDATE (для служебных пометок)."""

        return await self.session.get(
            EFHCTransferLog, int(transfer_id), with_for_update=True
        )

    async def list_cursor(
        self,
        *,
        limit: int,
        cursor: tuple[datetime, int] | None = None,
    ) -> list[EFHCTransferLog]:
        """Курсорная выборка журнала для админских отчётов."""

        stmt: Select[EFHCTransferLog] = (
            select(EFHCTransferLog)
            .order_by(EFHCTransferLog.created_at.desc(), EFHCTransferLog.id.desc())
            .limit(limit)
        )
        if cursor:
            ts, tid = cursor
            stmt = stmt.where(
                (EFHCTransferLog.created_at < ts)
                | ((EFHCTransferLog.created_at == ts) & (EFHCTransferLog.id < tid))
            )
        rows: Iterable[EFHCTransferLog] = await self.session.scalars(stmt)
        return list(rows)


__all__ = ["TransactionsCRUD"]

# ======================================================================
# Пояснения «для чайника»:
#   • CRUD не совершает денежных операций — только читает/пишет записи
#     efhc_transfers_log без изменения балансов.
#   • Повтор с тем же idempotency_key/tx_hash возвращает имеющуюся запись,
#     не создавая дублей.
#   • Пагинация только курсорная (created_at DESC, id DESC); OFFSET отсутствует.
# ======================================================================
