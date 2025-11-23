"""Модель банка EFHC с зеркальным логом переводов."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, Integer, Numeric
from sqlalchemy.orm import Mapped, mapped_column

from ..core.database_core import Base

DECIMAL = Numeric(30, 8, asdecimal=True)


class BankState(Base):
    """Состояние банка EFHC (может быть отрицательным по канону)."""

    __tablename__ = "bank_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    main_balance: Mapped[Decimal] = mapped_column(DECIMAL, default=Decimal("0"))
    bonus_balance: Mapped[Decimal] = mapped_column(DECIMAL, default=Decimal("0"))
    processed_with_deficit: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow
    )
