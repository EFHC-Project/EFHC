"""Admin referrals CRUD operations."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession


class AdminReferralsCRUD:
    """CRUD для админских проверок рефералок."""

    def __init__(self, session: AsyncSession):
        self.session = session
