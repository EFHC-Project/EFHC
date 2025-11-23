"""Admin stats CRUD operations."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession


class AdminStatsCRUD:
    """CRUD для админских агрегатов."""

    def __init__(self, session: AsyncSession):
        self.session = session
