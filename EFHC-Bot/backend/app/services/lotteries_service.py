"""Сервис лотерей: продажа билетов за EFHC."""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from ..crud.lotteries_crud import LotteriesCRUD
from ..models import LotteryTicket, User
from .transactions_service import TransactionsService


class LotteriesService:
    """Покупка билетов с идемпотентностью."""

    def __init__(self, session: AsyncSession):
        self.session = session
        self.crud = LotteriesCRUD(session)

    async def buy_ticket(
        self, lottery_id: int, user: User, ticket_price: Decimal, idempotency_key: str
    ) -> LotteryTicket:
        existing = await self.crud.get_ticket_by_key(idempotency_key)
        if existing:
            return existing

        tx_service = TransactionsService(self.session)
        await tx_service.debit_user(user, ticket_price, idempotency_key, reason="lottery_ticket")
        return await self.crud.create_ticket(lottery_id, user.id, idempotency_key)
