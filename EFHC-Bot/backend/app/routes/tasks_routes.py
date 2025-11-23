"""Task submission endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.deps import get_db
from ..core.security_core import require_idempotency_key
from ..models import Task, TaskSubmission, User
from ..services.transactions_service import TransactionsService

router = APIRouter()


class TaskSubmissionRequest(BaseModel):
    telegram_id: int
    task_id: int
    proof: str


class TaskSubmissionResponse(BaseModel):
    submission_id: int
    status: str


@router.post("/submit", response_model=TaskSubmissionResponse, dependencies=[Depends(require_idempotency_key)])
async def submit_task(
    payload: TaskSubmissionRequest,
    idempotency_key: str = Depends(require_idempotency_key),
    db: AsyncSession = Depends(get_db),
) -> TaskSubmissionResponse:
    user = await db.scalar(select(User).where(User.telegram_id == payload.telegram_id))
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    task = await db.get(Task, payload.task_id)
    if task is None or task.status != "active":
        raise HTTPException(status_code=400, detail="Task unavailable")
    submission = await db.scalar(select(TaskSubmission).where(TaskSubmission.idempotency_key == idempotency_key))
    if submission:
        return TaskSubmissionResponse(submission_id=submission.id, status=submission.status)
    submission = TaskSubmission(
        task_id=task.id,
        user_id=user.id,
        proof=payload.proof,
        status="pending",
        idempotency_key=idempotency_key,
    )
    db.add(submission)
    await db.flush()
    # Auto-approve to demonstrate bank payout per spec
    submission.status = "approved"
    service = TransactionsService(db)
    await service.credit_user(user, task.reward_efhc, idempotency_key=f"task:{idempotency_key}")
    return TaskSubmissionResponse(submission_id=submission.id, status=submission.status)
