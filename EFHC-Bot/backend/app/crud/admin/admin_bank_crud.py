"""Admin bank CRUD operations."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession


class AdminBankCRUD:
    """CRUD для админского доступа к банку."""

    def __init__(self, session: AsyncSession):
        self.session = session
