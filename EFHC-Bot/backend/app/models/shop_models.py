"""Модели витрины Shop и заказов."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from ..core.database_core import Base

DECIMAL = Numeric(30, 8, asdecimal=True)


class ShopItem(Base):
    """Карточка магазина (SKU, цена, активность)."""

    __tablename__ = "shop_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sku: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(128))
    price_efhc: Mapped[Decimal] = mapped_column(DECIMAL)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class ShopOrder(Base):
    """Заказ в магазине, связанный с оплатой TON/USDT."""

    __tablename__ = "shop_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    tx_hash: Mapped[str | None] = mapped_column(String(128), unique=True)
    item_sku: Mapped[str] = mapped_column(String(64))
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(32), default="received")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
