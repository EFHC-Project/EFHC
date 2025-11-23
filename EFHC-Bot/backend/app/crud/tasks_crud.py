"""Tasks CRUD operations."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Task, TaskSubmission


class TasksCRUD:
    """CRUD-операции по заданиям и отправкам."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def list_active(self) -> list[Task]:
        result = await self.session.scalars(select(Task).where(Task.status == "active"))
        return list(result)

    async def create_submission(
        self, task_id: int, user_id: int, proof: str, idempotency_key: str
    ) -> TaskSubmission:
        submission = TaskSubmission(
            task_id=task_id,
            user_id=user_id,
            proof=proof,
            idempotency_key=idempotency_key,
        )
        self.session.add(submission)
        await self.session.flush()
        return submission

    async def get_submission_by_key(self, key: str) -> TaskSubmission | None:
        return await self.session.scalar(
            select(TaskSubmission).where(TaskSubmission.idempotency_key == key)
        )
