"""Admin ads CRUD operations."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession


class AdminAdsCRUD:
    """CRUD для управления рекламными кампаниями."""

    def __init__(self, session: AsyncSession):
        self.session = session
