"""Лотереи и билеты."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from ..core.database_core import Base

DECIMAL = Numeric(30, 8, asdecimal=True)


class Lottery(Base):
    """Лотерея с ценой билета и статусом."""

    __tablename__ = "lotteries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(128))
    ticket_price: Mapped[Decimal] = mapped_column(DECIMAL)
    status: Mapped[str] = mapped_column(String(32), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class LotteryTicket(Base):
    """Билет лотереи, покупается только за EFHC."""

    __tablename__ = "lottery_tickets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lottery_id: Mapped[int] = mapped_column(ForeignKey("lotteries.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
