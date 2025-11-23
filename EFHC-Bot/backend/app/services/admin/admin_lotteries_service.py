"""Admin lotteries service."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from ...crud.admin.admin_lotteries_crud import AdminLotteriesCRUD


class AdminLotteriesService:
    """Сервис админки лотерей."""

    def __init__(self, session: AsyncSession):
        self.crud = AdminLotteriesCRUD(session)
