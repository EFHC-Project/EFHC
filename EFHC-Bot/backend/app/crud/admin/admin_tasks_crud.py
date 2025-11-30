# -*- coding: utf-8 -*-
# backend/app/crud/admin/admin_tasks_crud.py
# =============================================================================
# Назначение:
#   • Админский CRUD для задач и отправок: создание/обновление карточек задач,
#     курсорные выборки заявок для модерации, смена статусов без денежных операций.
#
# Канон/инварианты:
#   • Денежные начисления за задания выполняются сервисом через банк; CRUD только
#     фиксирует reward_amount/paid флаги, не списывая/начисляя EFHC.
#   • Только cursor-based пагинация (created_at DESC, id DESC); OFFSET запрещён.
#   • UNIQUE по code и (task_id,user_tg_id) соблюдается на уровне схемы, CRUD не создаёт дублей.
#
# ИИ-защита/самовосстановление:
#   • upsert_task() позволяет безопасно применять seed-скрипты без дублей по code.
#   • lock_submission() (делегирует TasksCRUD) должен использоваться перед выплатой/модерацией.
#
# Запреты:
#   • Не выполнять денежных операций или пересчётов лимитов в CRUD.
# =============================================================================
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Iterable

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models import Task, TaskSubmission


class AdminTasksCRUD:
    """Админский CRUD для задач и отправок (без денежной логики)."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def upsert_task(self, task: Task) -> Task:
        """Создать или обновить задачу по code (идемпотентно для seed)."""

        stmt: Select[Task] = select(Task).where(Task.code == task.code)
        existing = await self.session.scalar(stmt)
        if existing:
            existing.title = task.title
            existing.description = task.description
            existing.type = task.type
            existing.active = task.active
            existing.reward_bonus_efhc = task.reward_bonus_efhc
            existing.price_usd_hint = task.price_usd_hint
            existing.limit_per_user = task.limit_per_user
            existing.total_limit = task.total_limit
            existing.performed_count = task.performed_count
            existing.proof_type = task.proof_type
            existing.proof_hint = task.proof_hint
            await self.session.flush()
            return existing
        self.session.add(task)
        await self.session.flush()
        return task

    async def set_task_active(self, task_id: int, *, active: bool) -> Task | None:
        """Переключить флаг активности задачи (без изменения наград)."""

        task = await self.session.get(Task, int(task_id), with_for_update=True)
        if task is None:
            return None
        task.active = active
        await self.session.flush()
        return task

    async def list_tasks_cursor(
        self,
        *,
        limit: int,
        cursor: tuple[datetime, int] | None = None,
        active: bool | None = None,
    ) -> list[Task]:
        """Курсорная выдача задач для админки."""

        stmt: Select[Task] = select(Task).order_by(Task.created_at.desc(), Task.id.desc()).limit(limit)
        if active is True:
            stmt = stmt.where(Task.active.is_(True))
        elif active is False:
            stmt = stmt.where(Task.active.is_(False))
        if cursor:
            ts, tid = cursor
            stmt = stmt.where((Task.created_at < ts) | ((Task.created_at == ts) & (Task.id < tid)))

        rows: Iterable[Task] = await self.session.scalars(stmt)
        return list(rows)

    async def list_submissions_cursor(
        self,
        *,
        limit: int,
        cursor: tuple[datetime, int] | None = None,
        status: str | None = None,
    ) -> list[TaskSubmission]:
        """Курсорная выдача заявок для модерации (без выплат)."""

        stmt: Select[TaskSubmission] = (
            select(TaskSubmission)
            .order_by(TaskSubmission.created_at.desc(), TaskSubmission.id.desc())
            .limit(limit)
        )
        if status:
            stmt = stmt.where(TaskSubmission.status == status)
        if cursor:
            ts, sid = cursor
            stmt = stmt.where((TaskSubmission.created_at < ts) | ((TaskSubmission.created_at == ts) & (TaskSubmission.id < sid)))

        rows: Iterable[TaskSubmission] = await self.session.scalars(stmt)
        return list(rows)

    async def set_submission_status(
        self,
        submission_id: int,
        *,
        status: str,
        moderator_tg_id: int | None = None,
        reward_amount: Decimal | None = None,
        paid: bool | None = None,
        paid_tx_id: int | None = None,
    ) -> TaskSubmission | None:
        """Обновить заявку под блокировкой (без фактической выплаты)."""

        submission = await self.session.get(TaskSubmission, int(submission_id), with_for_update=True)
        if submission is None:
            return None
        submission.status = status
        if moderator_tg_id is not None:
            submission.moderator_tg_id = int(moderator_tg_id)
        if reward_amount is not None:
            submission.reward_amount_efhc = reward_amount
        if paid is not None:
            submission.paid = paid
        if paid_tx_id is not None:
            submission.paid_tx_id = int(paid_tx_id)
        await self.session.flush()
        return submission


__all__ = ["AdminTasksCRUD"]

# ============================================================================
# Пояснения «для чайника»:
#   • CRUD не начисляет EFHC — только фиксирует статусы/флаги оплаты.
#   • Все выборки и переключения выполняются под блокировкой/курсором, OFFSET не используется.
#   • Денежные операции выполняются сервисом через transactions_service и банк.
# ============================================================================
