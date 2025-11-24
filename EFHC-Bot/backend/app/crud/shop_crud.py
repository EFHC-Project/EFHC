"""Shop CRUD operations."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import ShopItem, ShopOrder


class ShopCRUD:
    """CRUD для витрины Shop и заказов."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def list_active(self) -> list[ShopItem]:
        result = await self.session.scalars(select(ShopItem).where(ShopItem.active.is_(True)))
        return list(result)

    async def create_order(self, user_id: int, sku: str, quantity: int) -> ShopOrder:
        order = ShopOrder(user_id=user_id, item_sku=sku, quantity=quantity)
        self.session.add(order)
        await self.session.flush()
        return order
