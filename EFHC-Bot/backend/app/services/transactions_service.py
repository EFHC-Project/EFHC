"""Centralised EFHC bank ledger with idempotent movements."""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.utils_core import quantize_decimal, utc_now
from ..models import BankState, EFHCTransferLog, User

BANK_ENTITY = "bank"
USER_ENTITY = "user"


class TransactionsService:
    """Provide atomic EFHC transfers respecting bank invariants."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def _get_bank(self) -> BankState:
        bank = await self.session.get(BankState, 1)
        if bank is None:
            bank = BankState(id=1)
            self.session.add(bank)
            await self.session.flush()
        return bank

    async def credit_user(
        self, user: User, amount: Decimal, idempotency_key: str, tx_hash: str | None = None
    ) -> EFHCTransferLog:
        amount = quantize_decimal(amount)
        bank = await self._get_bank()
        existing = await self.session.scalar(
            select(EFHCTransferLog).where(EFHCTransferLog.idempotency_key == idempotency_key)
        )
        if existing:
            return existing
        user.main_balance += amount
        bank.main_balance -= amount
        bank.processed_with_deficit = bank.main_balance < 0
        transfer = EFHCTransferLog(
            idempotency_key=idempotency_key,
            tx_hash=tx_hash,
            from_entity=BANK_ENTITY,
            to_entity=f"{USER_ENTITY}:{user.id}",
            amount=amount,
            processed_with_deficit=bank.processed_with_deficit,
            created_at=utc_now(),
        )
        self.session.add_all([user, bank, transfer])
        return transfer

    async def debit_user(
        self, user: User, amount: Decimal, idempotency_key: str, reason: str
    ) -> EFHCTransferLog:
        amount = quantize_decimal(amount)
        bank = await self._get_bank()
        existing = await self.session.scalar(
            select(EFHCTransferLog).where(EFHCTransferLog.idempotency_key == idempotency_key)
        )
        if existing:
            return existing
        if user.main_balance - amount < Decimal("0"):
            raise ValueError("User cannot go negative per EFHC invariants.")
        user.main_balance -= amount
        bank.main_balance += amount
        transfer = EFHCTransferLog(
            idempotency_key=idempotency_key,
            tx_hash=None,
            from_entity=f"{USER_ENTITY}:{user.id}",
            to_entity=BANK_ENTITY,
            amount=amount,
            processed_with_deficit=False,
            created_at=utc_now(),
        )
        self.session.add_all([user, bank, transfer])
        return transfer
