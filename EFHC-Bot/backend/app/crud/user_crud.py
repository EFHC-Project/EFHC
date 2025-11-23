"""User CRUD operations."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import User


class UserCRUD:
    """CRUD-операции для пользователей."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_by_telegram(self, telegram_id: int) -> User | None:
        return await self.session.scalar(select(User).where(User.telegram_id == telegram_id))

    async def create(self, telegram_id: int, ton_wallet: str | None) -> User:
        user = User(telegram_id=telegram_id, ton_wallet=ton_wallet)
        self.session.add(user)
        await self.session.flush()
        return user
