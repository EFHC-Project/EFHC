# -*- coding: utf-8 -*-
# backend/app/schemas/shop_schemas.py
# =============================================================================
# Назначение кода:
# Pydantic-схемы витрины магазина EFHC Bot (EFHC-пакеты, VIP-заявки, прочие SKU).
# Описывают формы входа/выхода для каталога, создания заказов, статусов оплат.
#
# Канон / инварианты:
# • Цена = 0 → карточка деактивирована (не продаётся).
# • Покупка EFHC/VIP и любых SKU с оплатой — денежная операция: Idempotency-Key
#   обязателен в роутере; деньги проводятся только через банковский сервис/вотчер.
# • Выдача NFT всегда вручную (статус PAID_PENDING_MANUAL); автодоставки нет.
# • Все суммы — Decimal(8) строками наружу; курсорная пагинация без OFFSET.
#
# ИИ-защиты:
# • Централизованные схемы каталога и заказов дают стабильную форму для фронта,
#   предотвращая расхождения при ошибках/ретраях.
# • client_nonce опционален как дополнительный слой идемпотентности на клиенте.
#
# Запреты:
# • Нет прямых движений денег в схемах; только формы данных.
# • Нет P2P и нет обратной конверсии EFHC→kWh.
# =============================================================================

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import List, Optional

from pydantic import BaseModel, Field, condecimal, constr, validator

from backend.app.schemas.common_schemas import CursorPage, OkMeta
from backend.app.deps import d8

# -----------------------------------------------------------------------------
# Каталог товаров
# -----------------------------------------------------------------------------


class ShopItemOut(BaseModel):
    """Карточка товара (EFHC-пакет или VIP-заявка)."""

    id: int = Field(..., description="ID товара")
    title: str = Field(..., description="Название карточки")
    description: Optional[str] = Field(None, description="Описание")
    price_efhc: Optional[Decimal] = Field(None, description="Цена в EFHC (если продаётся за EFHC)")
    price_ton: Optional[Decimal] = Field(None, description="Цена в TON (если продаётся внешне)")
    price_usdt: Optional[Decimal] = Field(None, description="Цена в USDT (если продаётся внешне)")
    is_active: bool = Field(..., description="Активна ли карточка (цена>0 и флаг is_active)")
    created_at: datetime = Field(..., description="Когда добавили карточку")

    @validator("price_efhc", "price_ton", "price_usdt", pre=True)
    def _q8(cls, v: Optional[Decimal]) -> Optional[Decimal]:
        if v is None:
            return None
        return d8(v)

    class Config:
        json_encoders = {Decimal: lambda v: str(d8(v))}


class ShopCatalogPage(CursorPage[ShopItemOut]):  # type: ignore[type-arg]
    """Каталог с курсорной пагинацией (items/next_cursor/etag)."""

    items: List[ShopItemOut]


# -----------------------------------------------------------------------------
# Создание заказов и статусы
# -----------------------------------------------------------------------------


class CreateOrderIn(BaseModel):
    """Вход создания заказа (денежная операция — Idempotency-Key обязателен)."""

    item_id: int = Field(..., description="ID товара")
    quantity: int = Field(..., gt=0, le=100, description="Количество штук (целое > 0)")
    telegram_id: int = Field(..., description="TG ID покупателя")
    client_nonce: Optional[constr(strip_whitespace=True, min_length=1, max_length=128)] = Field(
        None, description="Доп. нонс идемпотентности на клиенте"
    )


class CreateOrderOut(BaseModel):
    """Результат создания заказа с платёжными реквизитами."""

    meta: OkMeta = Field(default_factory=OkMeta)
    order_id: int = Field(..., description="ID созданного заказа")
    status: str = Field(..., description="Текущий статус заказа")
    memo: Optional[str] = Field(
        None, description="Сформированное MEMO для платежа (TON/USDT), если применимо"
    )
    ton_wallet: Optional[str] = Field(None, description="Кошелёк для оплаты TON")
    amount_ton: Optional[Decimal] = Field(None, description="Сумма к оплате в TON")
    amount_usdt: Optional[Decimal] = Field(None, description="Сумма к оплате в USDT")
    amount_efhc: Optional[Decimal] = Field(None, description="Сумма к оплате в EFHC (если внутренняя покупка)")
    idempotency_key: str = Field(..., description="Фактически применённый ключ идемпотентности")

    @validator("amount_ton", "amount_usdt", "amount_efhc", pre=True)
    def _q8_amounts(cls, v: Optional[Decimal]) -> Optional[Decimal]:
        if v is None:
            return None
        return d8(v)

    class Config:
        json_encoders = {Decimal: lambda v: str(d8(v))}


class OrderStatusOut(BaseModel):
    """Статус заказа (используется для поллинга UI)."""

    id: int = Field(..., description="ID заказа")
    status: str = Field(..., description="Статус заказа")
    tx_hash: Optional[str] = Field(None, description="Хеш внешнего платежа (TON/USDT), если есть")
    payment_method: Optional[str] = Field(None, description="Способ оплаты: ton/usdt/efhc")
    amount_ton: Optional[Decimal] = Field(None, description="Сумма TON")
    amount_usdt: Optional[Decimal] = Field(None, description="Сумма USDT")
    amount_efhc: Optional[Decimal] = Field(None, description="Сумма EFHC")
    memo: Optional[str] = Field(None, description="MEMO, если генерировалось")
    created_at: datetime = Field(..., description="Когда создан")
    updated_at: datetime = Field(..., description="Когда обновлён")

    @validator("amount_ton", "amount_usdt", "amount_efhc", pre=True)
    def _q8_amounts(cls, v: Optional[Decimal]) -> Optional[Decimal]:
        if v is None:
            return None
        return d8(v)

    class Config:
        json_encoders = {Decimal: lambda v: str(d8(v))}


# =============================================================================
# Пояснения:
# • Каталог — CursorPage[ShopItemOut]; цены = 0 или is_active=False скрывают товар.
# • Создание заказа — денежная операция: Idempotency-Key обязателен (в роутере),
#   сами движения денег делают сервисы/вотчер через банк, не схемы.
# • MEMO/ton_wallet возвращаются, чтобы пользователь мог оплатить через TON/USDT.
# • amount_efhc используется только для внутренних покупок (например, EFHC-пакет за EFHC).
# =============================================================================
