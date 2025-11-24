"""Panels CRUD operations."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Panel


class PanelsCRUD:
    """Работа с панелями: выборка активных и создание."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def list_active(self, user_id: int) -> list[Panel]:
        result = await self.session.scalars(
            select(Panel).where(Panel.user_id == user_id, Panel.status == "active")
        )
        return list(result)

    async def create(self, user_id: int, expires_at: datetime) -> Panel:
        panel = Panel(user_id=user_id, expires_at=expires_at, status="active")
        self.session.add(panel)
        await self.session.flush()
        return panel
