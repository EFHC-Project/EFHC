"""Admin bank service."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from ...crud.admin.admin_bank_crud import AdminBankCRUD


class AdminBankService:
    """Сервис чтения данных банка для админки."""

    def __init__(self, session: AsyncSession):
        self.crud = AdminBankCRUD(session)
