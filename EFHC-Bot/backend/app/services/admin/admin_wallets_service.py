"""Admin wallets service."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from ...crud.admin.admin_bank_crud import AdminBankCRUD


class AdminWalletsService:
    """Сервис для операций с банковским счётом и кошельками."""

    def __init__(self, session: AsyncSession):
        self.bank_crud = AdminBankCRUD(session)
