"""Сервис админских корректировок."""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from ..crud.order_crud import OrderCRUD
from ..models import AdjustmentOrder


class OrdersService:
    """Создание корректировок (журнал) без движения денег."""

    def __init__(self, session: AsyncSession):
        self.session = session
        self.crud = OrderCRUD(session)

    async def log_adjustment(self, direction: str, amount: Decimal, reason: str) -> AdjustmentOrder:
        order = AdjustmentOrder(direction=direction, amount=amount, reason=reason)
        return await self.crud.add(order)
