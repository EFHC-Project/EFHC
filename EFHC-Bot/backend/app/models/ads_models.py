"""Модели рекламных кампаний и показов."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from ..core.database_core import Base

DECIMAL = Numeric(30, 8, asdecimal=True)


class AdsCampaign(Base):
    """Рекламная кампания с бюджетом в EFHC."""

    __tablename__ = "ads_campaigns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(128))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    budget_efhc: Mapped[Decimal] = mapped_column(DECIMAL, default=Decimal("0"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class AdsImpression(Base):
    """Показ рекламы пользователю."""

    __tablename__ = "ads_impressions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("ads_campaigns.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
