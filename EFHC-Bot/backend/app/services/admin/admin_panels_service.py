"""Admin panels service."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from ...crud.admin.admin_panels_crud import AdminPanelsCRUD


class AdminPanelsService:
    """Сервис управления панелями в админке."""

    def __init__(self, session: AsyncSession):
        self.crud = AdminPanelsCRUD(session)
