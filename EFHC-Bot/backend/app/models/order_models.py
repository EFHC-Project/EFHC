# -*- coding: utf-8 -*-
# backend/app/models/order_models.py
# =============================================================================
# Назначение кода:
#   ORM-модель «Заказ в магазине» EFHC Bot:
#   • ShopOrder — единая запись для покупок EFHC-пакетов и NFT-заявок,
#                 которая проходит путь: PENDING → (PAID_AUTO | PAID_PENDING_MANUAL) → (APPROVED | REJECTED | CANCELLED).
#
# Канон/инварианты:
#   • Типы заказов: только 'EFHC' (пакеты EFHC с автодоставкой) и 'NFT' (только заявка, выдача вручную админом).
#   • Идемпотентность: idempotency_key UNIQUE — позволяет безопасно «создавать» заказ повторно (read-through).
#   • Привязка оплат из TON: tx_hash UNIQUE (входящие фиксируются в ton_inbox_logs; здесь просто ссылка).
#   • Денежные поля — Numeric(30,8) (Decimal с 8 знаками, округление вниз на уровне сервисов).
#   • Никакой автодоставки NFT — статус PAID_PENDING_MANUAL до ручного решения админа.
#
# ИИ-защиты:
#   • Курсорные индексы (created_at, id) — быстрые витрины без OFFSET.
#   • Поля expected_* и paid_* — позволяют сервису-«вотчеру» сверять сумму/валюту и корректно ставить статус.
#   • meta(JSONB) — безопасное расширение (SKU, TG, дополнительные проверки, флаги ретраев).
#
# Запреты:
#   • Модель НЕ изменяет балансы и не «автокредитует» EFHC — этим занимается банковский сервис.
#   • Никаких «суточных» ставок и логики генерации — заказ описывает только факт покупки/заявки.
# =============================================================================

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Numeric

from ..core.config_core import get_settings
from ..core.database_core import Base  # единый declarative Base проекта

settings = get_settings()
CORE_SCHEMA = settings.DB_SCHEMA_CORE  # например, "efhc_core"


class ShopOrder(Base):
    """
    Заказ в магазине (EFHC-пакеты и NFT).

    Поля:
      • user_id              — Telegram ID покупателя.
      • type                 — 'EFHC' | 'NFT' (других типов нет по канону).
      • status               — PENDING | PAID_AUTO | PAID_PENDING_MANUAL | APPROVED | REJECTED | CANCELLED.
      • sku                  — код позиции (например, 'EFHC_PACK_100' или 'NFT_VIP').
      • quantity             — количество (для EFHC-пакетов — объём пакета, для NFT — 1).
      • expected_amount      — ожидаемая сумма оплаты (в expected_currency).
      • expected_currency    — 'TON' | 'USDT' | 'EFHC' (для внутренних EFHC-оплат).
      • paid_amount          — фактически полученная сумма (заполняется вотчером/админом).
      • paid_currency        — валюта фактической оплаты.
      • tx_hash              — хеш транзакции TON (если есть), уникальный; может быть NULL до оплаты.
      • memo                 — полезная строка (например, "SKU:EFHC|Q:100|TG:12345").
      • idempotency_key      — ключ идемпотентности для безопасного создания/повторной отправки формы заказа.
      • processed_at         — момент финализации (когда статус стал одним из финальных: APPROVED/REJECTED/CANCELLED).
      • meta                 — произвольные расширения (JSONB).

    Примечания:
      • Для 'EFHC' после статуса PAID_AUTO сервис магазина инициирует списание с Банка и начисление EFHC пользователю.
      • Для 'NFT' любая успешная оплата переводит в PAID_PENDING_MANUAL, далее админ вручную решает (APPROVED/REJECTED).
    """

    __tablename__ = "shop_orders"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_shop_orders_idem_key"),
        UniqueConstraint("tx_hash", name="uq_shop_orders_tx_hash"),
        CheckConstraint("type IN ('EFHC','NFT')", name="ck_shop_orders_type_enum"),
        CheckConstraint(
            "status IN ('PENDING','PAID_AUTO','PAID_PENDING_MANUAL','APPROVED','REJECTED','CANCELLED')",
            name="ck_shop_orders_status_enum",
        ),
        CheckConstraint("quantity >= 0", name="ck_shop_orders_quantity_nonneg"),
        CheckConstraint("expected_amount >= 0", name="ck_shop_orders_expected_nonneg"),
        CheckConstraint("paid_amount IS NULL OR paid_amount >= 0", name="ck_shop_orders_paid_nonneg"),
        {"schema": CORE_SCHEMA},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Владелец/покупатель
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)

    # Тип и статус заказа
    type: Mapped[str] = mapped_column(String(8), nullable=False, index=True)   # 'EFHC' | 'NFT'
    status: Mapped[str] = mapped_column(String(24), nullable=False, index=True, default="PENDING", server_default="PENDING")

    # Описание позиции и ожидаемые параметры оплаты
    sku: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    quantity: Mapped[str] = mapped_column(Numeric(30, 8), nullable=False, default="0", server_default="0")

    expected_amount: Mapped[str] = mapped_column(Numeric(30, 8), nullable=False, default="0", server_default="0")
    expected_currency: Mapped[Optional[str]] = mapped_column(String(8), nullable=True, index=True)  # 'TON' | 'USDT' | 'EFHC'

    # Фактическая оплата (после вотчера/админа)
    paid_amount: Mapped[Optional[str]] = mapped_column(Numeric(30, 8), nullable=True)
    paid_currency: Mapped[Optional[str]] = mapped_column(String(8), nullable=True, index=True)

    # TON-платёжные атрибуты
    tx_hash: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    memo: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)

    # Идемпотентность создания/обновления
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False, index=True)

    # Финализация и служебные атрибуты
    processed_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    meta: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB, nullable=True)

    # Таймстемпы
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return f"<ShopOrder id={self.id} user={self.user_id} type={self.type} status={self.status} sku={self.sku}>"


# Индексы под курсор/витрины без OFFSET
Index("ix_shop_orders_created_id", ShopOrder.created_at, ShopOrder.id,
      postgresql_using="btree", schema=CORE_SCHEMA)
Index("ix_shop_orders_user_created", ShopOrder.user_id, ShopOrder.created_at,
      postgresql_using="btree", schema=CORE_SCHEMA)
Index("ix_shop_orders_type_status", ShopOrder.type, ShopOrder.status,
      postgresql_using="btree", schema=CORE_SCHEMA)


__all__ = [
    "ShopOrder",
]
# =============================================================================
# Пояснения «для чайника»:
#   • Почему 'NFT' не доставляется автоматически?
#     Канон запрещает автодоставку NFT. Даже при успешной оплате заказ переводится
#     лишь в PAID_PENDING_MANUAL, далее админ вручную выполняет выдачу и меняет статус.
#
#   • Зачем idempotency_key у заказа?
#     Чтобы фронтенд/интеграции могли безопасно повторить создание/обновление заказа
#     (например, при сетевых сбоях) без риска дубликатов. При конфликте ключа сервис
#     возвращает уже созданный заказ (read-through).
#
#   • Что с валютами?
#     expected_currency/paid_currency позволяют вотчеру валидировать оплату (TON/USDT/EFHC)
#     и принимать корректное решение о статусе. Для внутренних оплат EFHC валютой может быть 'EFHC'.
#
#   • Где связь с входящими транзакциями TON?
#     Первичный учёт идёт в ton_inbox_logs (уникальный tx_hash). Здесь мы храним tx_hash как ссылку
#     для удобной корреляции и аудита. Именно вотчер/сервис магазина совмещает события и заказы.
# =============================================================================
