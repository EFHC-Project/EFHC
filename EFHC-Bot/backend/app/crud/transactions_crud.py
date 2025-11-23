"""Transactions CRUD operations."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import EFHCTransferLog


class TransactionsCRUD:
    """Доступ к журналу переводов EFHC."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_by_idempotency(self, key: str) -> EFHCTransferLog | None:
        return await self.session.scalar(
            select(EFHCTransferLog).where(EFHCTransferLog.idempotency_key == key)
        )

    async def add(self, log: EFHCTransferLog) -> EFHCTransferLog:
        self.session.add(log)
        await self.session.flush()
        return log
