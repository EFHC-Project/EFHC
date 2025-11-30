# -*- coding: utf-8 -*-
# backend/app/crud/tasks_crud.py
# =============================================================================
# Назначение:
#   • CRUD-слой для задач (tasks) и отправок (task_submissions) в пользовательском
#     контуре: курсорные выборки, идемпотентное создание отправок, смена статусов.
#   • Денежные начисления за задания выполняются только через банковский сервис;
#     CRUD не двигает EFHC и не трогает балансы.
#
# Канон/инварианты:
#   • Статусы submission: pending|approved|rejected|auto_paid (строка, задаётся
#     сервисом/админкой); CRUD не генерирует выплату.
#   • Один пользователь может иметь не более одной отправки на задачу
#     (UNIQUE task_id + user_tg_id) — соблюдаем read-through при создании.
#   • Только cursor-based пагинация (created_at DESC, id DESC); OFFSET запрещён.
#
# ИИ-защита/самовосстановление:
#   • create_submission_if_absent() возвращает существующую попытку для пары
#     (task_id, user_tg_id), предотвращая дубли в гонках.
#   • lock_submission() использует FOR UPDATE перед изменением статуса/флага оплаты,
#     чтобы исключить рассинхрон с банковским сервисом.
#
# Запреты:
#   • Никаких денежных операций внутри CRUD: ни начислений, ни списаний.
#   • Не трогаем idempotency_key банковских операций; здесь только заявки/статусы.
# =============================================================================
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Iterable

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models import Task, TaskSubmission


class TasksCRUD:
    """CRUD-обёртка для tasks и task_submissions без денежной логики."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_task(self, task_id: int) -> Task | None:
        """Получить задачу по id."""

        return await self.session.get(Task, int(task_id))

    async def get_task_by_code(self, code: str) -> Task | None:
        """Найти задачу по уникальному коду."""

        stmt: Select[Task] = select(Task).where(Task.code == code)
        return await self.session.scalar(stmt)

    async def list_active_tasks_cursor(
        self,
        *,
        limit: int,
        cursor: tuple[datetime, int] | None = None,
        task_type: str | None = None,
    ) -> list[Task]:
        """Вернуть активные задачи для пользовательской витрины (курсор)."""

        stmt: Select[Task] = (
            select(Task)
            .where(Task.active.is_(True))
            .order_by(Task.created_at.desc(), Task.id.desc())
            .limit(limit)
        )
        if task_type:
            stmt = stmt.where(Task.type == task_type)
        if cursor:
            ts, tid = cursor
            stmt = stmt.where((Task.created_at < ts) | ((Task.created_at == ts) & (Task.id < tid)))

        rows: Iterable[Task] = await self.session.scalars(stmt)
        return list(rows)

    async def create_submission_if_absent(
        self,
        *,
        task_id: int,
        user_tg_id: int,
        proof_text: str | None,
        proof_url: str | None,
        status: str = "pending",
    ) -> TaskSubmission:
        """
        Идемпотентно создать отправку на выполнение задания.

        Повторное обращение для той же пары task_id+user возвращает существующую запись.
        Денежные операции отсутствуют.
        """

        existing = await self.get_submission(task_id=task_id, user_tg_id=user_tg_id)
        if existing:
            return existing

        submission = TaskSubmission(
            task_id=int(task_id),
            user_tg_id=int(user_tg_id),
            proof_text=proof_text,
            proof_url=proof_url,
            status=status,
        )
        self.session.add(submission)
        await self.session.flush()
        return submission

    async def get_submission(self, *, task_id: int, user_tg_id: int) -> TaskSubmission | None:
        """Найти отправку пользователя по задаче."""

        stmt: Select[TaskSubmission] = select(TaskSubmission).where(
            TaskSubmission.task_id == int(task_id),
            TaskSubmission.user_tg_id == int(user_tg_id),
        )
        return await self.session.scalar(stmt)

    async def lock_submission(self, submission_id: int) -> TaskSubmission | None:
        """Получить submission под FOR UPDATE перед модерацией/выплатой."""

        return await self.session.get(TaskSubmission, int(submission_id), with_for_update=True)

    async def update_submission_status(
        self,
        submission_id: int,
        *,
        status: str,
        moderator_tg_id: int | None = None,
        reward_amount: Decimal | None = None,
        paid: bool | None = None,
        paid_tx_id: int | None = None,
    ) -> TaskSubmission | None:
        """
        Обновить статус/флаги выплаты заявки под блокировкой.

        Денежные действия (само начисление EFHC) выполняются сервисом; CRUD фиксирует факт.
        """

        submission = await self.lock_submission(submission_id)
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

    async def list_submissions_cursor(
        self,
        *,
        limit: int,
        cursor: tuple[datetime, int] | None = None,
        status: str | None = None,
        task_id: int | None = None,
        user_tg_id: int | None = None,
    ) -> list[TaskSubmission]:
        """Курсорная выборка заявок (используется модерацией/сервисами)."""

        stmt: Select[TaskSubmission] = (
            select(TaskSubmission)
            .order_by(TaskSubmission.created_at.desc(), TaskSubmission.id.desc())
            .limit(limit)
        )
        if status:
            stmt = stmt.where(TaskSubmission.status == status)
        if task_id:
            stmt = stmt.where(TaskSubmission.task_id == int(task_id))
        if user_tg_id:
            stmt = stmt.where(TaskSubmission.user_tg_id == int(user_tg_id))
        if cursor:
            ts, sid = cursor
            stmt = stmt.where(
                (TaskSubmission.created_at < ts)
                | ((TaskSubmission.created_at == ts) & (TaskSubmission.id < sid))
            )

        rows: Iterable[TaskSubmission] = await self.session.scalars(stmt)
        return list(rows)


__all__ = ["TasksCRUD"]

# ============================================================================
# Пояснения «для чайника»:
#   • CRUD не начисляет EFHC и не валидирует лимиты задач — это обязанность сервисов.
#   • Идемпотентность отправок обеспечивается UNIQUE(task_id,user_tg_id) и
#     create_submission_if_absent().
#   • Для списков используются курсоры (created_at DESC, id DESC); OFFSET не применяется.
# ============================================================================
