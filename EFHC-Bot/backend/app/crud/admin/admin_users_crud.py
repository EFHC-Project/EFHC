"""Admin users CRUD operations."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession


class AdminUsersCRUD:
    """CRUD для админского просмотра пользователей."""

    def __init__(self, session: AsyncSession):
        self.session = session
