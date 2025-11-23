"""Сервис магазина EFHC."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from ..crud.shop_crud import ShopCRUD
from ..models import ShopItem, ShopOrder


class ShopService:
    """Выдача витрины и создание заказов."""

    def __init__(self, session: AsyncSession):
        self.session = session
        self.crud = ShopCRUD(session)

    async def list_items(self) -> list[ShopItem]:
        return await self.crud.list_active()

    async def create_order(self, user_id: int, sku: str, quantity: int) -> ShopOrder:
        return await self.crud.create_order(user_id, sku, quantity)
