"""Admin tasks service."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from ...crud.admin.admin_tasks_crud import AdminTasksCRUD


class AdminTasksService:
    """Сервис управления заданиями в админке."""

    def __init__(self, session: AsyncSession):
        self.crud = AdminTasksCRUD(session)
