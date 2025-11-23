"""Сервис отчётности (краткие агрегаты)."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import EFHCTransferLog, Panel, User


class ReportsService:
    """Быстрые агрегаты для админских экранов."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def totals(self) -> dict[str, int]:
        users = await self.session.scalar(select(func.count(User.id))) or 0
        panels = await self.session.scalar(select(func.count(Panel.id))) or 0
        transfers = await self.session.scalar(select(func.count(EFHCTransferLog.id))) or 0
        return {"users": users, "panels": panels, "transfers": transfers}
