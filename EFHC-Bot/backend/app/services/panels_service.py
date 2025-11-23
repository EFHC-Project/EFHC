"""Сервис управления панелями."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from ..core.utils_core import quantize_decimal, utc_now
from ..crud.panels_crud import PanelsCRUD
from ..models import Panel, User
from .transactions_service import TransactionsService

PANEL_PRICE_EFHC = Decimal("100")
PANEL_LIFETIME_DAYS = 180
MAX_ACTIVE_PANELS = 1000


class PanelsService:
    """Покупка и управление панелями с учётом лимитов."""

    def __init__(self, session: AsyncSession):
        self.session = session
        self.panels_crud = PanelsCRUD(session)

    async def purchase_panel(self, user: User, idempotency_key: str) -> Panel:
        active_panels = await self.panels_crud.list_active(user.id)
        if len(active_panels) >= MAX_ACTIVE_PANELS:
            raise ValueError("Active panel limit reached (1000).")

        price = quantize_decimal(PANEL_PRICE_EFHC)
        tx_service = TransactionsService(self.session)
        await tx_service.debit_user(user, price, idempotency_key, reason="panel_purchase")

        expires_at = utc_now() + timedelta(days=PANEL_LIFETIME_DAYS)
        return await self.panels_crud.create(user.id, expires_at)
