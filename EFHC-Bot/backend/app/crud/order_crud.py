"""Order CRUD operations."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from ..models import AdjustmentOrder


class OrderCRUD:
    """CRUD для админских корректировок."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def add(self, order: AdjustmentOrder) -> AdjustmentOrder:
        self.session.add(order)
        await self.session.flush()
        return order
