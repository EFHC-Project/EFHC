"""Admin tasks CRUD operations."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession


class AdminTasksCRUD:
    """CRUD для админских действий над заданиями."""

    def __init__(self, session: AsyncSession):
        self.session = session
