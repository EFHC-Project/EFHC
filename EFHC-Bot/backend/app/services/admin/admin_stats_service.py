"""Admin stats service."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from ...crud.admin.admin_stats_crud import AdminStatsCRUD


class AdminStatsService:
    """Сервис для админских метрик."""

    def __init__(self, session: AsyncSession):
        self.crud = AdminStatsCRUD(session)
