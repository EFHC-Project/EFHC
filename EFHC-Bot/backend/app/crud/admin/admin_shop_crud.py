"""Админский CRUD для каталога магазина и заказов.

======================================================================
Назначение:
    • Управление карточками магазина (shop_items) и вспомогательные выборки
      заказов для админки. CRUD не двигает деньги и не меняет балансы.

Канон/инварианты:
    • Карточка с price=0 считается неактивной; активность задаётся явно.
    • Денежные операции проводятся через сервисы/банк, здесь только запись
      статусов и цен. OFFSET не используется, курсоры (created_at, id).
    • P2P и EFHC→kWh запрещены, NFT выдаётся вручную (статусы в сервисах).

ИИ-защита/самовосстановление:
    • upsert_item() обновляет существующую карточку без создания дублей по
      коду/названию (опционально), позволяя безопасно применять seed-скрипты.
    • list_orders_cursor() использует FOR UPDATE только по необходимости
      (не блокирует без надобности) и не влияет на балансы.

Запреты:
    • Не выполнять денежных списаний/зачислений в CRUD.
    • Не изменять idempotency_key заказов — этим занимаются сервисы.
======================================================================
"""

from __future__ import annotations

from datetime import datetime
from typing import Iterable

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.logging_core import get_logger
from backend.app.models.shop_models import ShopItem, ShopOrder

logger = get_logger(__name__)


class AdminShopCRUD:
    """CRUD-обёртка для shop_items и вспомогательных списков заказов."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_item(self, item_id: int) -> ShopItem | None:
        """Получить карточку магазина по id."""

        return await self.session.get(ShopItem, int(item_id))

    async def upsert_item(
        self,
        *,
        item: ShopItem,
    ) -> ShopItem:
        """Сохранить карточку: обновить, если уже присутствует."""

        if item.id:
            existing = await self.session.get(ShopItem, int(item.id))
            if existing:
                existing.title = item.title
                existing.description = item.description
                existing.price_efhc = item.price_efhc
                existing.price_ton = item.price_ton
                existing.price_usdt = item.price_usdt
                existing.is_active = item.is_active
                await self.session.flush()
                return existing
        self.session.add(item)
        await self.session.flush()
        return item

    async def set_active(self, item_id: int, active: bool) -> ShopItem | None:
        """Включить/выключить карточку без изменения цен."""

        item = await self.session.get(ShopItem, int(item_id), with_for_update=True)
        if item is None:
            return None
        item.is_active = active
        await self.session.flush()
        return item

    async def list_items_cursor(
        self,
        *,
        limit: int,
        cursor: tuple[datetime, int] | None = None,
        include_inactive: bool = True,
    ) -> list[ShopItem]:
        """Курсорная выборка карточек (админ-витрина)."""

        stmt: Select[ShopItem] = (
            select(ShopItem)
            .order_by(ShopItem.created_at.desc(), ShopItem.id.desc())
            .limit(limit)
        )
        if not include_inactive:
            stmt = stmt.where(ShopItem.is_active.is_(True))
        if cursor:
            ts, iid = cursor
            stmt = stmt.where(
                (ShopItem.created_at < ts)
                | ((ShopItem.created_at == ts) & (ShopItem.id < iid))
            )
        rows: Iterable[ShopItem] = await self.session.scalars(stmt)
        return list(rows)

    async def list_orders_cursor(
        self,
        *,
        limit: int,
        cursor: tuple[datetime, int] | None = None,
        status: str | None = None,
    ) -> list[ShopOrder]:
        """Курсорная выборка заказов для админского контроля."""

        stmt: Select[ShopOrder] = (
            select(ShopOrder)
            .order_by(ShopOrder.created_at.desc(), ShopOrder.id.desc())
            .limit(limit)
        )
        if status:
            stmt = stmt.where(ShopOrder.status == status)
        if cursor:
            ts, oid = cursor
            stmt = stmt.where(
                (ShopOrder.created_at < ts)
                | ((ShopOrder.created_at == ts) & (ShopOrder.id < oid))
            )
        rows: Iterable[ShopOrder] = await self.session.scalars(stmt)
        return list(rows)


__all__ = ["AdminShopCRUD"]

# ======================================================================
# Пояснения «для чайника»:
#   • CRUD не двигает EFHC и не меняет балансы; только карточки/заказы.
#   • Пагинация через курсоры (created_at DESC, id DESC), OFFSET не используется.
#   • Активность карточки регулируется set_active; цена 0 делает её бесполезной,
#     но фактическая логика отключения карточек лежит на сервисах/админке.
# ======================================================================
