# -*- coding: utf-8 -*-
# backend/app/models/shop_models.py
# =============================================================================
# Назначение кода:
#   ORM-модели домена «Магазин» EFHC Bot:
#   • каталог товаров (опционально) — ShopItem;
#   • заказы пользователя — ShopOrder (EFHC-пакеты и NFT-заявки).
#
# Канон/инварианты:
#   • Денежные/энергетические величины — Numeric(30,8) (Decimal с 8 знаками, округление вниз в сервисах).
#   • EFHC-пакеты: автодоставка после оплаты (TON → вотчер → банк → пользователь).
#   • NFT: никаких авто-выдач; только заявка (ручная обработка админом), статус PAID_PENDING_MANUAL.
#   • Идемпотентность входящих оплат — по tx_hash (UNIQUE в shop_orders и в ton_inbox_logs).
#   • Никаких P2P: только «банк ↔ пользователь» через сервисы. Модели денег не двигают.
#
# ИИ-защиты:
#   • Курсорные индексы (created_at, id) для быстрых витрин/истории.
#   • Поля audit/extra для безопасного расширения без миграций (JSONB).
#   • user_id допускает NULL (если покупатель ещё не сопоставлен; вотчер позже свяжет по MEMO).
#
# Запреты:
#   • Никаких «суточных» ставок/полей.
#   • Никаких скрытых «балансов» в магазине; деньги двигает только банковский сервис.
# =============================================================================

from __future__ import annotations

from datetime import datetime
from enum import Enum
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


# -----------------------------------------------------------------------------
# Типы и статусы заказов
# -----------------------------------------------------------------------------
class ShopOrderType(str, Enum):
    """Тип заказа магазина."""
    EFHC = "EFHC"        # пакет EFHC (автодоставка после оплаты)
    NFT  = "NFT"         # заявка на NFT (только ручная выдача админом)


class ShopOrderStatus(str, Enum):
    """Статус заказа магазина (унифицирован для EFHC и NFT)."""
    PENDING              = "PENDING"               # создан, ожидает оплаты
    PAID_AUTO            = "PAID_AUTO"             # оплачен (EFHC): автодоставка выполнена
    PAID_PENDING_MANUAL  = "PAID_PENDING_MANUAL"   # оплачен (NFT): ждёт ручной выдачи админом
    APPROVED             = "APPROVED"              # админ подтвердил (обычно для NFT)
    REJECTED             = "REJECTED"              # отклонён админом (возврат решается вручную)
    CANCELED             = "CANCELED"              # отменён пользователем/системой


# -----------------------------------------------------------------------------
# Каталог товаров (опционально)
# -----------------------------------------------------------------------------
class ShopItem(Base):
    """
    Каталожная позиция магазина (опционально для витрины).
    В простейшем сценарии EFHC/NFT позиции могут быть зашиты конфигом, а эта таблица —
    для расширения ассортимента через админку.
    """
    __tablename__ = "shop_items"
    __table_args__ = (
        UniqueConstraint("sku", name="uq_shop_items_sku"),
        {"schema": CORE_SCHEMA},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Уникальный артикул (напр., "EFHC_PACK_100", "NFT_VIP")
    sku: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    title: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)

    # Бизнес-поля (для EFHC-пакетов quantity_efhc > 0; для NFT можно держать метаданные в extra)
    quantity_efhc: Mapped[Optional[str]] = mapped_column(Numeric(30, 8), nullable=True)
    is_active: Mapped[bool] = mapped_column(nullable=False, default=True, server_default="true")

    extra: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB, nullable=True)

    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return f"<ShopItem id={self.id} sku={self.sku} active={self.is_active}>"


Index("ix_shop_items_created_id", ShopItem.created_at, ShopItem.id, postgresql_using="btree", schema=CORE_SCHEMA)


# -----------------------------------------------------------------------------
# Заказ магазина
# -----------------------------------------------------------------------------
class ShopOrder(Base):
    """
    Заказ магазина. Создаётся фронтом/ботом (PENDING), затем оплачивается в TON.
    Вотчер (watcher_service) по MEMO/tx_hash сопоставляет оплату и меняет статус.

    Семантика ключевых полей:
      • type:
          - EFHC — покупка пакета EFHC (автодоставка после оплаты → PAID_AUTO).
          - NFT  — заявка на NFT (после оплаты → PAID_PENDING_MANUAL; далее админ APPROVED/REJECTED).
      • status — см. ShopOrderStatus.
      • user_id — может быть NULL до момента сопоставления (если известен только TG из MEMO).
      • tx_hash — уникальный идентификатор входящего платежа TON (устанавливается вотчером).
      • memo — исходный MEMO из TON для аудита (SKU:..., EFHC<tgid> и т.п.).
      • quantity_efhc — количество EFHC в заказе (для NFT обычно NULL).
      • expected_amount — ожидаемая сумма в базовой валюте оплаты (например, TON); контроль в вотчере/админке.
      • idempotency_key — внутренний ключ идемпотентности создания/изменения заказа (не денежная операция, но полезно).
    """

    __tablename__ = "shop_orders"
    __table_args__ = (
        UniqueConstraint("tx_hash", name="uq_shop_orders_tx_hash"),
        CheckConstraint("(type = 'EFHC' OR type = 'NFT')", name="ck_shop_orders_type_enum"),
        CheckConstraint("quantity_efhc IS NULL OR quantity_efhc >= 0", name="ck_shop_orders_qty_nonneg"),
        CheckConstraint("expected_amount IS NULL OR expected_amount >= 0", name="ck_shop_orders_expected_nonneg"),
        {"schema": CORE_SCHEMA},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Идентификация покупателя: может быть NULL до сопоставления вотчером/админом
    user_id: Mapped[Optional[int]] = mapped_column(BigInteger, index=True, nullable=True, comment="Telegram ID пользователя")

    type: Mapped[str] = mapped_column(String(8), nullable=False, index=True)     # 'EFHC' | 'NFT'
    status: Mapped[str] = mapped_column(String(24), nullable=False, index=True, default=ShopOrderStatus.PENDING.value)

    # Бизнес-параметры
    quantity_efhc: Mapped[Optional[str]] = mapped_column(Numeric(30, 8), nullable=True)
    expected_amount: Mapped[Optional[str]] = mapped_column(Numeric(30, 8), nullable=True, comment="Ожидаемая сумма оплаты (TON и т.п.)")

    # TON-сопоставление
    tx_hash: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    memo: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)

    # Идемпотентность уровня заказов (не денежная, но полезна для повторных запросов фронта/бота)
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)

    # Произвольные данные: SKU, ценовые параметры, источник рекламной кампании и др.
    extra: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB, nullable=True)

    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    paid_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    fulfilled_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return f"<ShopOrder id={self.id} type={self.type} status={self.status} user={self.user_id} tx={self.tx_hash}>"



# Индексы под курсор/выборки
Index("ix_shop_orders_created_id", ShopOrder.created_at, ShopOrder.id, postgresql_using="btree", schema=CORE_SCHEMA)
Index("ix_shop_orders_user_created", ShopOrder.user_id, ShopOrder.created_at, postgresql_using="btree", schema=CORE_SCHEMA)
Index("ix_shop_orders_status", ShopOrder.status, postgresql_using="btree", schema=CORE_SCHEMA)
Index("ix_shop_orders_idem", ShopOrder.idempotency_key, postgresql_using="btree", schema=CORE_SCHEMA)


# -----------------------------------------------------------------------------
# Экспорт
# -----------------------------------------------------------------------------
__all__ = [
    "ShopItem",
    "ShopOrder",
    "ShopOrderType",
    "ShopOrderStatus",
]
# =============================================================================
# Пояснения «для чайника»:
#   • Почему user_id может быть NULL?
#     MEMO может содержать только TG-ID (EFHC<tgid> или SKU:...|TG:<id>), а привязки аккаунта к TON ещё нет.
#     Вотчер сначала парсит MEMO, создаёт/находит заказ и затем связывает его с пользователем, когда это возможно.
#
#   • Чем отличается EFHC от NFT в статусах?
#     EFHC после оплаты уходит в PAID_AUTO (автодоставка — банк зачисляет пользователю EFHC).
#     NFT после оплаты становится PAID_PENDING_MANUAL — админ вручную подтверждает выдачу NFT (APPROVED/REJECTED).
#
#   • Зачем expected_amount и extra?
#     expected_amount помогает валидировать сумму платежа TON (анти-ошибки/анти-фрод).
#     extra — гибкое поле для SKU/курсов/кампаний, чтобы не менять схему при расширениях.
# =============================================================================
