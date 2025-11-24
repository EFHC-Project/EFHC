# -*- coding: utf-8 -*-
# backend/app/models/lottery_models.py
# =============================================================================
# Назначение кода:
#   SQLAlchemy-модели подсистемы лотерей EFHC Bot: лотереи, билеты, агрегаты
#   по пользователям, результаты розыгрыша и заявки на NFT-приз.
#
# Канон/инварианты:
#   • Денежная логика отсутствует — модели только описывают структуру данных.
#   • Точность денег — Decimal(30, 8), округление вниз реализует банковский слой.
#   • Курсорная пагинация: индексы по (created_at, id) и по ticket_id.
#   • Уникальность: один результат на лотерею; уникальные номера билетов в лотерее.
#
# ИИ-защита/самовосстановление:
#   • Явные CHECK/ENUM-ограничения по статусам предотвращают «мусорные» состояния.
#   • Индексы под типичные выборки: активные лотереи, мои билеты, очередность розыгрыша.
#   • Чёткое разделение сущностей: Lottery (карточка), LotteryTicket (экземпляр билета),
#     LotteryUserStat (агрегаты по пользователю), LotteryResult (итог), LotteryNFTClaim (ручная выдача NFT).
#
# Запреты:
#   • Никаких полей/логики для авто-выдачи NFT и для P2P-операций — это запрещено каноном.
#   • Никаких суточных ставок/энергетических расчётов — лотереи не знают про генерацию kWh.
# =============================================================================

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP

from ..core.config_core import get_settings
from ..core.database_core import Base

_settings = get_settings()
SCHEMA = getattr(_settings, "DB_SCHEMA_CORE", "efhc_core")

# -----------------------------------------------------------------------------
# Константы статусов/типов (строковые ENUM)
# -----------------------------------------------------------------------------

LOTTERY_STATUS_ENUM = ("draft", "active", "closed", "completed")
PRIZE_TYPE_ENUM = ("EFHC", "NFT")
NFT_CLAIM_STATUS_ENUM = ("pending", "approved", "rejected")

# =============================================================================
# МОДЕЛИ
# =============================================================================

class Lottery(Base):
    """
    Карточка лотереи: параметры розыгрыша и агрегаты по продаже билетов.
    """
    __tablename__ = "lotteries"
    __table_args__ = (
        # Кардинальные ограничения
        CheckConstraint(f"status IN {LOTTERY_STATUS_ENUM}", name="lottery_status_check"),
        CheckConstraint(f"prize_type IN {PRIZE_TYPE_ENUM}", name="lottery_prize_type_check"),
        # Бизнес-ограничения
        CheckConstraint("total_tickets >= 0", name="lottery_total_tickets_nonneg"),
        CheckConstraint("tickets_sold >= 0", name="lottery_tickets_sold_nonneg"),
        CheckConstraint("tickets_sold <= total_tickets", name="lottery_sold_le_total"),
        # Индексы под курсоры и витрины
        Index("ix_lottery_status", "status"),
        Index("ix_lottery_created_cursor", "created_at", "id"),
        {"schema": SCHEMA},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    title: Mapped[str] = mapped_column(String(200), nullable=False)

    # Приз: EFHC (число в EFHC, хранится строкой-Decimal) или NFT (описание/код)
    prize_type: Mapped[str] = mapped_column(String(8), nullable=False)
    prize_value: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)

    # Цена билета (EFHC, Decimal(30,8))
    ticket_price: Mapped[Decimal] = mapped_column(
        Numeric(30, 8),
        nullable=False,
    )

    # Ограничения по участникам/билетам
    max_participants: Mapped[int] = mapped_column(Integer, nullable=False)
    max_tickets_per_user: Mapped[int] = mapped_column(Integer, nullable=False)

    # Агрегаты и статусы
    total_tickets: Mapped[int] = mapped_column(Integer, nullable=False)       # обычно = max_participants
    tickets_sold: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")
    auto_draw: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # Тайминги
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True), nullable=True)

    # Связи (ленивые; используются редко, поэтому не навязываем join'ы)
    tickets: Mapped[list["LotteryTicket"]] = relationship(
        back_populates="lottery", cascade="all, delete-orphan", lazy="raise_on_sql"
    )
    result: Mapped[Optional["LotteryResult"]] = relationship(
        back_populates="lottery", uselist=False, cascade="all, delete-orphan", lazy="raise_on_sql"
    )
    nft_claims: Mapped[list["LotteryNFTClaim"]] = relationship(
        back_populates="lottery", cascade="all, delete-orphan", lazy="raise_on_sql"
    )


class LotteryTicket(Base):
    """
    Экземпляр билета лотереи. ticket_id нумеруется последовательно внутри лотереи
    (1..total_tickets). Уникальность (lottery_id, ticket_id) гарантирует отсутствие дублей.
    """
    __tablename__ = "lottery_tickets"
    __table_args__ = (
        UniqueConstraint("lottery_id", "ticket_id", name="uq_lottery_ticket"),
        Index("ix_ticket_owner", "owner_telegram_id", "lottery_id", "ticket_id"),
        Index("ix_ticket_lottery_cursor", "lottery_id", "ticket_id"),
        {"schema": SCHEMA},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    lottery_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey(f"{SCHEMA}.lotteries.id", ondelete="CASCADE"),
        nullable=False,
    )

    # Порядковый номер в рамках лотереи (1..N)
    ticket_id: Mapped[int] = mapped_column(Integer, nullable=False)

    # Владелец билета — Telegram ID
    owner_telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )

    # Для явных переходов по связям (опционально, избегаем лишних JOIN по умолчанию)
    lottery: Mapped["Lottery"] = relationship(back_populates="tickets", lazy="raise_on_sql")


class LotteryUserStat(Base):
    """
    Агрегаты по пользователю в конкретной лотерее (сколько билетов купил).
    Уникальная строка на пару (lottery_id, telegram_id).
    """
    __tablename__ = "lottery_user_stats"
    __table_args__ = (
        UniqueConstraint("lottery_id", "telegram_id", name="uq_lottery_user_stat"),
        Index("ix_lottery_user_stat_updated", "lottery_id", "updated_at"),
        {"schema": SCHEMA},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    lottery_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey(f"{SCHEMA}.lotteries.id", ondelete="CASCADE"),
        nullable=False,
    )
    telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False)

    tickets_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )

    lottery: Mapped["Lottery"] = relationship(lazy="raise_on_sql")


class LotteryResult(Base):
    """
    Результат розыгрыша: один результат на лотерею. Содержит выигравший билет и победителя.
    """
    __tablename__ = "lottery_results"
    __table_args__ = (
        UniqueConstraint("lottery_id", name="uq_lottery_result_unique"),
        Index("ix_lottery_result_created", "created_at"),
        {"schema": SCHEMA},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    lottery_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey(f"{SCHEMA}.lotteries.id", ondelete="CASCADE"),
        nullable=False,
    )

    winning_ticket_id: Mapped[int] = mapped_column(Integer, nullable=False)
    winner_telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    completed_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)

    lottery: Mapped["Lottery"] = relationship(back_populates="result", lazy="raise_on_sql")


class LotteryNFTClaim(Base):
    """
    Заявка на вручную выдаваемый NFT-приз. Автоматической выдачи НЕТ (канон).
    """
    __tablename__ = "lottery_nft_claims"
    __table_args__ = (
        CheckConstraint(f"status IN {NFT_CLAIM_STATUS_ENUM}", name="nft_claim_status_check"),
        Index("ix_nft_claim_status", "status"),
        {"schema": SCHEMA},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    lottery_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey(f"{SCHEMA}.lotteries.id", ondelete="CASCADE"),
        nullable=False,
    )

    winner_telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False)

    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")

    # Адрес кошелька победителя в нужной сети (если известен/подтвержден вручную)
    wallet_address: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)

    # Произвольные детали — номер коллекции, ID заявки в админке, комментарии модератора и т. п.
    meta: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )

    lottery: Mapped["Lottery"] = relationship(back_populates="nft_claims", lazy="raise_on_sql")


# =============================================================================
# Пояснения (для чайника):
#   • ticket_price хранится как Numeric(30,8) — фактическое округление/режим
#     округления всегда реализует банковский сервис (transactions_service).
#   • Для курсорной пагинации:
#       – по лотереям: индекс (created_at, id) и статус,
#       – по билетам: (lottery_id, ticket_id) и (owner_telegram_id, lottery_id, ticket_id).
#   • Один LotteryResult на лотерею: уникальность по lottery_id.
#   • Авто-выдачи NFT нет — заявки фиксируются в LotteryNFTClaim со статусом pending,
#     а реальная выдача выполняется вручную в админке.
# =============================================================================
