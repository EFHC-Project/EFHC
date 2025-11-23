"""Admin referral service."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from ...crud.admin.admin_referrals_crud import AdminReferralsCRUD


class AdminReferralService:
    """Сервис админки рефералок."""

    def __init__(self, session: AsyncSession):
        self.crud = AdminReferralsCRUD(session)
