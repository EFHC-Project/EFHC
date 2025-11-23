"""Endpoints for managing solar panels."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.deps import get_db
from ..core.security_core import require_idempotency_key
from ..core.utils_core import utc_now
from ..models import Panel, User
from ..services.transactions_service import TransactionsService

router = APIRouter()

PANEL_PRICE_EFHC = Decimal("100")
PANEL_LIFETIME_DAYS = 180
PANEL_LIMIT = 1000


class PanelPurchaseRequest(BaseModel):
    telegram_id: int


class PanelResponse(BaseModel):
    id: int
    status: str
    expires_at: str


@router.post("/purchase", response_model=PanelResponse, dependencies=[Depends(require_idempotency_key)])
async def purchase_panel(
    payload: PanelPurchaseRequest,
    idempotency_key: str = Depends(require_idempotency_key),
    db: AsyncSession = Depends(get_db),
) -> PanelResponse:
    user = await db.scalar(select(User).where(User.telegram_id == payload.telegram_id))
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    panel_count = await db.scalar(select(func.count()).select_from(Panel).where(Panel.user_id == user.id))
    if panel_count and panel_count >= PANEL_LIMIT:
        raise HTTPException(status_code=400, detail="Panel limit reached")
    service = TransactionsService(db)
    await service.debit_user(user, PANEL_PRICE_EFHC, idempotency_key=idempotency_key, reason="panel_purchase")
    expires_at = utc_now() + timedelta(days=PANEL_LIFETIME_DAYS)
    panel = Panel(user_id=user.id, expires_at=expires_at, status="active")
    db.add(panel)
    await db.flush()
    return PanelResponse(id=panel.id, status=panel.status, expires_at=panel.expires_at.isoformat())
