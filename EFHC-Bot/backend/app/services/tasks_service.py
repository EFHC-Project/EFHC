"""Сервис заданий: выдача списка и прием отправок."""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from ..crud.tasks_crud import TasksCRUD
from ..models import TaskSubmission, User
from .transactions_service import TransactionsService


class TasksService:
    """Приём доказательств и начисление наград через банк."""

    def __init__(self, session: AsyncSession):
        self.session = session
        self.tasks_crud = TasksCRUD(session)

    async def submit_task(
        self, task_id: int, user: User, proof: str, idempotency_key: str, reward: Decimal
    ) -> TaskSubmission:
        existing = await self.tasks_crud.get_submission_by_key(idempotency_key)
        if existing:
            return existing

        submission = await self.tasks_crud.create_submission(task_id, user.id, proof, idempotency_key)
        tx_service = TransactionsService(self.session)
        await tx_service.credit_user(user, reward, idempotency_key=idempotency_key)
        return submission
