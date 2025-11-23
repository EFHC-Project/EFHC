"""Lottery endpoints for ticket purchases."""

from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.deps import get_db
from ..core.security_core import require_idempotency_key
from ..models import Lottery, LotteryTicket, User
from ..services.transactions_service import TransactionsService

router = APIRouter()


class TicketPurchaseRequest(BaseModel):
    telegram_id: int
    lottery_id: int


class TicketResponse(BaseModel):
    ticket_id: int


@router.post("/tickets", response_model=TicketResponse, dependencies=[Depends(require_idempotency_key)])
async def buy_ticket(
    payload: TicketPurchaseRequest,
    idempotency_key: str = Depends(require_idempotency_key),
    db: AsyncSession = Depends(get_db),
) -> TicketResponse:
    user = await db.scalar(select(User).where(User.telegram_id == payload.telegram_id))
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    lottery = await db.get(Lottery, payload.lottery_id)
    if lottery is None or lottery.status != "active":
        raise HTTPException(status_code=400, detail="Lottery unavailable")
    service = TransactionsService(db)
    await service.debit_user(user, Decimal(lottery.ticket_price), idempotency_key=idempotency_key, reason="lottery_ticket")
    ticket = LotteryTicket(lottery_id=lottery.id, user_id=user.id, idempotency_key=idempotency_key)
    db.add(ticket)
    await db.flush()
    return TicketResponse(ticket_id=ticket.id)
