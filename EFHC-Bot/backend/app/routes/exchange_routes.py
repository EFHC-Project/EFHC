"""Exchange kWh to EFHC endpoints."""

from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.security_core import require_idempotency_key
from ..core.deps import get_db
from ..core.utils_core import quantize_decimal
from ..models import User
from ..services.transactions_service import TransactionsService

router = APIRouter()


class ExchangeRequest(BaseModel):
    telegram_id: int
    kwh_amount: Decimal


class ExchangeResponse(BaseModel):
    efhc_received: str
    user_main_balance: str
    user_available_kwh: str


@router.post("/kwh-to-efhc", response_model=ExchangeResponse, dependencies=[Depends(require_idempotency_key)])
async def exchange_kwh_to_efhc(
    payload: ExchangeRequest,
    idempotency_key: str = Depends(require_idempotency_key),
    db: AsyncSession = Depends(get_db),
) -> ExchangeResponse:
    user = await db.scalar(select(User).where(User.telegram_id == payload.telegram_id))
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    kwh = quantize_decimal(payload.kwh_amount)
    if user.available_kwh < kwh:
        raise HTTPException(status_code=400, detail="Insufficient kWh for exchange")
    user.available_kwh -= kwh
    service = TransactionsService(db)
    transfer = await service.credit_user(user, kwh, idempotency_key=idempotency_key)
    await db.flush()
    return ExchangeResponse(
        efhc_received=str(transfer.amount),
        user_main_balance=str(user.main_balance),
        user_available_kwh=str(user.available_kwh),
    )
