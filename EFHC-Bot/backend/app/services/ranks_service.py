"""Сервис рейтингов."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from ..crud.ranks_crud import RanksCRUD
from ..models import RatingSnapshot


class RanksService:
    """Работа со снимками рейтинга."""

    def __init__(self, session: AsyncSession):
        self.session = session
        self.crud = RanksCRUD(session)

    async def latest(self, limit: int = 100) -> list[RatingSnapshot]:
        return await self.crud.list_latest(limit)
