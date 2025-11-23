"""Lotteries CRUD operations."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Lottery, LotteryTicket


class LotteriesCRUD:
    """CRUD по лотереям и билетам."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def list_active(self) -> list[Lottery]:
        result = await self.session.scalars(select(Lottery).where(Lottery.status == "active"))
        return list(result)

    async def create_ticket(self, lottery_id: int, user_id: int, key: str) -> LotteryTicket:
        ticket = LotteryTicket(lottery_id=lottery_id, user_id=user_id, idempotency_key=key)
        self.session.add(ticket)
        await self.session.flush()
        return ticket

    async def get_ticket_by_key(self, key: str) -> LotteryTicket | None:
        return await self.session.scalar(
            select(LotteryTicket).where(LotteryTicket.idempotency_key == key)
        )
