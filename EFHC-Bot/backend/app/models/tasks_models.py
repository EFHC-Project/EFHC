"""Модели витрины заданий и отправленных доказательств."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..core.database_core import Base

DECIMAL = Numeric(30, 8, asdecimal=True)


class Task(Base):
    """Задание, публикуемое в витрине."""

    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(256))
    description: Mapped[str] = mapped_column(Text())
    reward_efhc: Mapped[Decimal] = mapped_column(DECIMAL)
    status: Mapped[str] = mapped_column(String(32), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class TaskSubmission(Base):
    """Отправка доказательства по заданию с идемпотентным ключом."""

    __tablename__ = "task_submissions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    proof: Mapped[str] = mapped_column(Text())
    status: Mapped[str] = mapped_column(String(32), default="pending")
    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
