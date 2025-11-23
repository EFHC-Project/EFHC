"""Admin panels CRUD operations."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession


class AdminPanelsCRUD:
    """CRUD для админского управления панелями."""

    def __init__(self, session: AsyncSession):
        self.session = session
