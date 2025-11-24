"""Ranks CRUD operations."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import RatingSnapshot


class RanksCRUD:
    """CRUD для рейтинговых снимков."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def list_latest(self, limit: int = 100) -> list[RatingSnapshot]:
        result = await self.session.scalars(
            select(RatingSnapshot).order_by(RatingSnapshot.created_at.desc()).limit(limit)
        )
        return list(result)

    async def add_snapshot(self, snapshot: RatingSnapshot) -> RatingSnapshot:
        self.session.add(snapshot)
        await self.session.flush()
        return snapshot
