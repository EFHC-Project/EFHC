"""Admin withdrawals service."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from ...crud.admin.admin_withdrawals_crud import AdminWithdrawalsCRUD


class AdminWithdrawalsService:
    """Сервис обработки выводов в админке."""

    def __init__(self, session: AsyncSession):
        self.crud = AdminWithdrawalsCRUD(session)
