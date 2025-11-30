# -*- coding: utf-8 -*-
# backend/app/crud/shop_crud.py
# =============================================================================
# Назначение:
#   • Пользовательский CRUD для витрины магазина (shop_items) без денежных операций.
#   • Обеспечивает курсорные выборки активных карточек и получение по SKU/ID.
#
# Канон/инварианты:
#   • Цена/количество EFHC на карточке — справочные данные; сами покупки идут
#     через shop_orders/банк. CRUD не двигает балансы и не создаёт заказы.
#   • Активность карточки определяется флагом is_active и бизнес-логикой сервиса
#     (например, price=0 ⇒ деактивировать). OFFSET запрещён, только курсоры.
#
# ИИ-защита/самовосстановление:
#   • list_active_cursor() возвращает только is_active=true; сервис может безопасно
#     повторять вызовы без риска дублей.
#
# Запреты:
#   • Никаких денежных действий или генерации заказов в CRUD.
#   • Не перезаписывать цены/статусы — это зона админского CRUD/сервисов.
# =============================================================================
from __future__ import annotations

from datetime import datetime
from typing import Iterable

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.shop_models import ShopItem


class ShopCRUD:
    """CRUD-обёртка для пользовательской витрины shop_items."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_by_id(self, item_id: int) -> ShopItem | None:
        """Получить карточку по id (без блокировки)."""

        return await self.session.get(ShopItem, int(item_id))

    async def get_by_sku(self, sku: str) -> ShopItem | None:
        """Найти карточку по SKU (уникальное поле)."""

        stmt: Select[ShopItem] = select(ShopItem).where(ShopItem.sku == sku)
        return await self.session.scalar(stmt)

    async def list_active_cursor(
        self,
        *,
        limit: int,
        cursor: tuple[datetime, int] | None = None,
    ) -> list[ShopItem]:
        """Курсорная выборка активных карточек (is_active=true)."""

        stmt: Select[ShopItem] = (
            select(ShopItem)
            .where(ShopItem.is_active.is_(True))
            .order_by(ShopItem.created_at.desc(), ShopItem.id.desc())
            .limit(limit)
        )
        if cursor:
            ts, iid = cursor
            stmt = stmt.where((ShopItem.created_at < ts) | ((ShopItem.created_at == ts) & (ShopItem.id < iid)))

        rows: Iterable[ShopItem] = await self.session.scalars(stmt)
        return list(rows)


__all__ = ["ShopCRUD"]

# ============================================================================
# Пояснения «для чайника»:
#   • CRUD не создаёт shop_orders и не двигает EFHC — только витрина карточек.
#   • Для списков — курсор (created_at DESC, id DESC); OFFSET не используется.
#   • Активность карточки регулируется админским CRUD/сервисами, здесь только чтение.
# ============================================================================
