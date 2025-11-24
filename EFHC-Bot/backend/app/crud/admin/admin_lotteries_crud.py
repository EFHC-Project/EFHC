"""Admin lotteries CRUD operations."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession


class AdminLotteriesCRUD:
    """CRUD для админских операций с лотереями."""

    def __init__(self, session: AsyncSession):
        self.session = session
