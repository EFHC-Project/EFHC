# -*- coding: utf-8 -*-
# backend/app/models/user_models.py
# =============================================================================
# Назначение кода:
#   ORM-модель домена «Пользователи» EFHC Bot:
#   • User — профиль пользователя Telegram с жёсткими банковскими инвариантами.
#
# Канон/инварианты:
#   • Единственный идентификатор — telegram_id (BigInt), уникален в системе.
#   • Денежные/энергетические поля — Numeric(30,8), округление вниз выполняют СЕРВИСЫ.
#   • Жёсткий запрет «минуса» у пользователя: main_balance, bonus_balance, available_kwh,
#     total_generated_kwh — всегда ≥ 0; доступная энергия не превышает тотал.
#   • VIP-ставка определяется наличием NFT (is_vip — флаг факта, а не «пожизненный статус»).
#
# ИИ-защиты:
#   • Индексы под курсорную пагинацию и быстрые витрины (created_at,id), рейтинг (total_generated_kwh),
#     поиск по ton_wallet; meta(JSONB) — безопасное расширение без миграций.
#   • Поля last_seen_at/last_sync_at — позволяют «догонять» состояние и отслеживать активность.
#
# Запреты:
#   • Модель НЕ выполняет денежных операций (никаких автосписаний/начислений).
#   • Никаких «суточных» полей/логики — только пер-секунда в сервисах.
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


class User(Base):
    """
    Пользователь EFHC Bot (Telegram).

    Поля:
      • telegram_id            — уникальный идентификатор Telegram (PK by business-logic).
      • username               — ник (может быть NULL/меняться).
      • is_active              — мягкая деактивация (бан/архив).
      • is_vip                 — флаг VIP (производный от наличия NFT при последней проверке).
      • ton_wallet             — один активный кошелёк TON (NULL, если не привязан).
      • main_balance           — основной баланс EFHC (Decimal(30,8), ≥ 0).
      • bonus_balance          — бонусный баланс EFHC (Decimal(30,8), ≥ 0).
      • available_kwh          — доступная к обмену энергия (Decimal(30,8), ≥ 0).
      • total_generated_kwh    — суммарная сгенерированная энергия (Decimal(30,8), ≥ 0).
      • last_seen_at           — последняя активность (например, визит в WebApp).
      • last_sync_at           — последняя «принудительная синхронизация» с бэкендом.
      • meta                   — произвольные тех.атрибуты (язык, источник онбординга и т.п.).
    """

    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("telegram_id", name="uq_users_telegram"),
        UniqueConstraint("ton_wallet", name="uq_users_ton_wallet"),
        # Жёсткий запрет на «минус» у пользователя + согласованность available ≤ total:
        CheckConstraint("main_balance >= 0", name="ck_users_main_balance_nonneg"),
        CheckConstraint("bonus_balance >= 0", name="ck_users_bonus_balance_nonneg"),
        CheckConstraint("available_kwh >= 0", name="ck_users_available_kwh_nonneg"),
        CheckConstraint("total_generated_kwh >= 0", name="ck_users_total_generated_kwh_nonneg"),
        CheckConstraint("available_kwh <= total_generated_kwh", name="ck_users_available_le_total"),
        {"schema": CORE_SCHEMA},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Бизнес-идентификатор (Telegram)
    telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    username: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)

    # Статусы
    is_active: Mapped[bool] = mapped_column(nullable=False, default=True, server_default="true", index=True)
    is_vip: Mapped[bool] = mapped_column(nullable=False, default=False, server_default="false", index=True)

    # Привязка кошелька TON (один активный на пользователя)
    ton_wallet: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)

    # Балансы EFHC (Decimal(30,8)) и энергия
    main_balance: Mapped[str] = mapped_column(Numeric(30, 8), nullable=False, default="0", server_default="0")
    bonus_balance: Mapped[str] = mapped_column(Numeric(30, 8), nullable=False, default="0", server_default="0")
    available_kwh: Mapped[str] = mapped_column(Numeric(30, 8), nullable=False, default="0", server_default="0")
    total_generated_kwh: Mapped[str] = mapped_column(Numeric(30, 8), nullable=False, default="0", server_default="0")

    # Активность/синхронизация
    last_seen_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True), nullable=True, index=True)
    last_sync_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True), nullable=True, index=True)

    # Расширения
    meta: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB, nullable=True)

    # Таймстемпы
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return f"<User tg={self.telegram_id} vip={self.is_vip} main={self.main_balance} bonus={self.bonus_balance}>"


# Индексы под курсор/витрины/рейтинг
Index("ix_users_created_id", User.created_at, User.id, postgresql_using="btree", schema=CORE_SCHEMA)
Index("ix_users_total_kwh", User.total_generated_kwh, User.id, postgresql_using="btree", schema=CORE_SCHEMA)
Index("ix_users_available_kwh", User.available_kwh, User.id, postgresql_using="btree", schema=CORE_SCHEMA)

__all__ = [
    "User",
]
# =============================================================================
# Пояснения «для чайника»:
#   • Почему telegram_id — уникален отдельно от PK id?
#     Внутренний id — технический ключ таблицы. Telegram ID используется как «бизнес-ид», его
#     уникальность защищаем явно (и индексы по нему быстрые).
#
#   • Почему уникальный ton_wallet?
#     Канон: у пользователя может быть один активный TON-адрес. Уникальность предотвращает
#     пересечение кошельков между разными пользователями. Несколько NULL допустимы (PostgreSQL).
#
#   • Можно ли пользователю уйти «в минус»?
#     Нет. Это жёстко запрещено CHECK-ограничениями и логикой сервисов. Если исторически
#     минус обнаружен (например, импорт старых данных), сервисы блокируют покупки за EFHC
#     до выхода в ноль/плюс, но разрешают конвертацию kWh→EFHC и пополнения.
# =============================================================================
