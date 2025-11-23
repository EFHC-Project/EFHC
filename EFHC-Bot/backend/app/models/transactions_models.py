"""Лог переводов EFHC с идемпотентностью и TON-входящими."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from ..core.database_core import Base

DECIMAL = Numeric(30, 8, asdecimal=True)


class EFHCTransferLog(Base):
    """Зеркальная запись движений банк ↔ пользователь (идемпотентно)."""

    __tablename__ = "efhc_transfers_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    tx_hash: Mapped[str | None] = mapped_column(String(128), index=True)
    from_entity: Mapped[str] = mapped_column(String(64))
    to_entity: Mapped[str] = mapped_column(String(64))
    amount: Mapped[Decimal] = mapped_column(DECIMAL)
    processed_with_deficit: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class TonInboxLog(Base):
    """Журнал входящих переводов из TON с ретраями."""

    __tablename__ = "ton_inbox_logs"

    tx_hash: Mapped[str] = mapped_column(String(128), primary_key=True)
    memo: Mapped[str] = mapped_column(String(256))
    status: Mapped[str] = mapped_column(String(32), default="received")
    retries_count: Mapped[int] = mapped_column(Integer, default=0)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
