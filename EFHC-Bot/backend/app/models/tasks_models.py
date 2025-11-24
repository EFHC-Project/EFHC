# -*- coding: utf-8 -*-
"""
Модели таблиц заданий (tasks) и отправок (task_submissions).

Назначение:
  • tasks — справочник заданий, которые видят пользователи.
  • task_submissions — попытки выполнения заданий пользователями, хранит доказательства, статусы, выплату EFHC.

Схема БД берётся из settings.DB_SCHEMA_TASKS (например, efhc_tasks).
Все денежные поля — Decimal(18, 8). Строковые индексы и уникальные ограничения заданы.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, declarative_mixin, mapped_column, relationship

from ..core.config_core import settings
from ..core.database_core import Base


SCHEMA_TASKS = settings.DB_SCHEMA_TASKS  # например, "efhc_tasks"


@declarative_mixin
class TimestampMixin:
    """Простая примесь для created_at / updated_at."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=datetime.utcnow,
        nullable=False,
        comment="Когда запись создана (UTC).",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
        comment="Когда запись обновлена (UTC).",
    )


class Task(Base, TimestampMixin):
    """
    EFHC задание, которое создаёт админ.
    Пользователь его видит в списке и может выполнить (прислать доказательство/кнопка «Готово»).
    """

    __tablename__ = "tasks"
    __table_args__ = (
        UniqueConstraint("code", name="uq_tasks_code"),
        CheckConstraint("reward_bonus_efhc >= 0", name="chk_tasks_reward_nonnegative"),
        CheckConstraint("limit_per_user >= 0", name="chk_tasks_limit_per_user_nonnegative"),
        CheckConstraint("(total_limit IS NULL) OR (total_limit >= 0)", name="chk_tasks_total_limit_nonnegative"),
        {"schema": SCHEMA_TASKS},
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),  # совместимость с sqlite в тестах
        primary_key=True,
        autoincrement=True,
        comment="Идентификатор задания.",
    )

    code: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        unique=False,
        index=True,
        comment="Код задания (машинное имя), уникален в рамках всей таблицы.",
    )

    title: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="Короткий заголовок задания.",
    )

    description: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="Описание задания, инструкции для пользователя.",
    )

    type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="custom",
        index=True,
        comment="Тип задания (произвольная метка), например: subscribe/join/visit/custom/…",
    )

    active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        index=True,
        comment="Видимость задания пользователям.",
    )

    reward_bonus_efhc: Mapped[Decimal] = mapped_column(
        Numeric(18, 8),
        nullable=False,
        default=Decimal(str(settings.TASK_REWARD_BONUS_EFHC_DEFAULT)),
        comment="Сколько бонусных EFHC начислять за выполнение.",
    )

    price_usd_hint: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(18, 8),
        nullable=True,
        comment="Подсказка админам: сколько стоит это задание в USD (для экономики).",
    )

    limit_per_user: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        comment="Сколько раз один пользователь может выполнить задание (0 — нельзя, >=1 — ограничение).",
    )

    total_limit: Mapped[Optional[int]] = mapped_column(
        Integer,
        nullable=True,
        comment="Глобальный лимит выполнений для всех пользователей (NULL — без лимита).",
    )

    performed_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        comment="Сколько выполнений уже одобрено (для отслеживания тотального лимита).",
    )

    proof_type: Mapped[Optional[str]] = mapped_column(
        String(50),
        nullable=True,
        comment="Какое доказательство требуется: url/screenshot/text/none/… (на усмотрение админа).",
    )

    proof_hint: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
        comment="Подсказка пользователю, что именно приложить в качестве доказательства.",
    )

    submissions: Mapped[list["TaskSubmission"]] = relationship(
        "TaskSubmission",
        back_populates="task",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


Index(
    "ix_tasks_active_type",
    Task.active,
    Task.type,
    schema=SCHEMA_TASKS,
)


class TaskSubmission(Base, TimestampMixin):
    """
    Отправка выполнения задания пользователем.
    Здесь хранится доказательство и статус модерации, а также факт выплаты EFHC.
    """

    __tablename__ = "task_submissions"
    __table_args__ = (
        UniqueConstraint("task_id", "user_tg_id", name="uq_task_submissions_task_user_once"),
        CheckConstraint("reward_amount_efhc >= 0", name="chk_task_submissions_reward_nonnegative"),
        {"schema": SCHEMA_TASKS},
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
        comment="Идентификатор отправки.",
    )

    task_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey(f"{SCHEMA_TASKS}.tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="FK → tasks.id",
    )

    # В проекте идентификатор пользователя — Telegram ID (int). Храним прямое поле без FK на efhc_core.users.
    user_tg_id: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        index=True,
        comment="Telegram ID пользователя, который выполнил задание.",
    )

    status: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        default="pending",  # pending|approved|rejected|auto_paid
        index=True,
        comment="Статус модерации/выплаты.",
    )

    proof_text: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="Текстовое доказательство/комментарий от пользователя.",
    )

    proof_url: Mapped[Optional[str]] = mapped_column(
        String(1024),
        nullable=True,
        comment="URL-доказательство (скрин в облаке, пост, профиль и т.п.).",
    )

    reward_amount_efhc: Mapped[Decimal] = mapped_column(
        Numeric(18, 8),
        nullable=False,
        default=Decimal("0.00000000"),
        comment="Сколько EFHC начислено (фиксируется при одобрении).",
    )

    paid: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        index=True,
        comment="Флаг фактической выплаты EFHC.",
    )

    paid_tx_id: Mapped[Optional[int]] = mapped_column(
        BigInteger,
        nullable=True,
        index=True,
        comment="ID транзакции EFHC в внутренней учётной системе (если есть).",
    )

    moderator_tg_id: Mapped[Optional[int]] = mapped_column(
        BigInteger,
        nullable=True,
        comment="Telegram ID модератора/админа, который принял решение по заявке.",
    )

    task: Mapped["Task"] = relationship(
        "Task",
        back_populates="submissions",
    )


Index(
    "ix_task_submissions_user_status",
    TaskSubmission.user_tg_id,
    TaskSubmission.status,
    schema=SCHEMA_TASKS,
)

