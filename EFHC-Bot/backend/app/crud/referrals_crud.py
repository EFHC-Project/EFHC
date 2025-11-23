"""Referrals CRUD operations."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Referral


class ReferralsCRUD:
    """CRUD для реферальных связей."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(self, user_id: int, parent_id: int | None) -> Referral:
        ref = Referral(user_id=user_id, parent_id=parent_id)
        self.session.add(ref)
        await self.session.flush()
        return ref

    async def get_by_user(self, user_id: int) -> Referral | None:
        return await self.session.scalar(select(Referral).where(Referral.user_id == user_id))
