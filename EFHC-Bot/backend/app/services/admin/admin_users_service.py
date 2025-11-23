"""Admin users service."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from ...crud.admin.admin_users_crud import AdminUsersCRUD


class AdminUsersService:
    """Сервис просмотра и блокировок пользователей в админке."""

    def __init__(self, session: AsyncSession):
        self.crud = AdminUsersCRUD(session)
