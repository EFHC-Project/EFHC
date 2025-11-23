"""Admin shop CRUD operations."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession


class AdminShopCRUD:
    """CRUD для админского каталога Shop."""

    def __init__(self, session: AsyncSession):
        self.session = session
