"""User models по канону EFHC.

В этой секции описываем таблицу users — главный источник правды по
балансам, VIP-статусу и энергии. Все числовые поля Decimal(30, 8) и
инициализируются нулями, чтобы пользователь никогда не уходил в минус.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..core.database_core import Base

DECIMAL = Numeric(30, 8, asdecimal=True)


class User(Base):
    """Пользователь EFHC с отдельными балансами и метаданными."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    ton_wallet: Mapped[str | None] = mapped_column(String(128))
    main_balance: Mapped[Decimal] = mapped_column(DECIMAL, default=Decimal("0"))
    bonus_balance: Mapped[Decimal] = mapped_column(DECIMAL, default=Decimal("0"))
    available_kwh: Mapped[Decimal] = mapped_column(DECIMAL, default=Decimal("0"))
    total_generated_kwh: Mapped[Decimal] = mapped_column(DECIMAL, default=Decimal("0"))
    is_vip: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow
    )

    panels = relationship("Panel", back_populates="user", cascade="all, delete-orphan")
    referrals = relationship("Referral", back_populates="user", cascade="all, delete-orphan")
    rating_snapshots = relationship(
        "RatingSnapshot", back_populates="user", cascade="all, delete-orphan"
    )
