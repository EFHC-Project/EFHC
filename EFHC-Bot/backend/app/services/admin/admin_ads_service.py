"""Admin ads service."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from ...crud.admin.admin_ads_crud import AdminAdsCRUD


class AdminAdsService:
    """Тонкая обёртка над CRUD для админки рекламы."""

    def __init__(self, session: AsyncSession):
        self.crud = AdminAdsCRUD(session)
