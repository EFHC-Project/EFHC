"""Сервис рефералок."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from ..crud.referrals_crud import ReferralsCRUD
from ..models import Referral


class ReferralService:
    """Регистрация и получение реферальных связей."""

    def __init__(self, session: AsyncSession):
        self.session = session
        self.crud = ReferralsCRUD(session)

    async def ensure_referral(self, user_id: int, parent_id: int | None) -> Referral:
        existing = await self.crud.get_by_user(user_id)
        if existing:
            return existing
        return await self.crud.create(user_id, parent_id)
