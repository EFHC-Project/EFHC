"""Admin withdrawals CRUD operations."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession


class AdminWithdrawalsCRUD:
    """CRUD для админских запросов на вывод."""

    def __init__(self, session: AsyncSession):
        self.session = session
