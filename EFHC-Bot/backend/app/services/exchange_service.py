"""Сервис обмена kWh → EFHC по курсу 1:1."""

from __future__ import annotations

from decimal import Decimal

from ..core.utils_core import quantize_decimal
from ..models import User
from .transactions_service import TransactionsService


class ExchangeService:
    """Конверсия доступной энергии пользователя в EFHC."""

    def __init__(self, tx_service: TransactionsService):
        self.tx_service = tx_service

    async def convert_kwh_to_efhc(self, user: User, amount_kwh: Decimal, key: str) -> Decimal:
        amount = quantize_decimal(amount_kwh)
        if amount <= Decimal("0"):
            raise ValueError("Amount must be positive")
        if user.available_kwh < amount:
            raise ValueError("Not enough kWh to exchange")

        user.available_kwh -= amount
        await self.tx_service.credit_user(user, amount, idempotency_key=key, tx_hash=None)
        return amount
