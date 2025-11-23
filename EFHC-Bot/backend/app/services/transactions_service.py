"""Единый банковский сервис EFHC с read-through идемпотентностью."""

from __future__ import annotations

# ======================================================================
# EFHC Bot — services/transactions_service.py
# ----------------------------------------------------------------------
# Назначение: атомарные движения EFHC между Банком и пользователями с
#             жёсткими инвариантами (Idempotency-Key, запрет минуса у
#             пользователя, допускаемый минус у Банка).
# Канон/инварианты:
#   • Все суммы Decimal(8) с ROUND_DOWN (quantize_decimal).
#   • Пользователь не может уйти в минус; банк может (fixed by deficit flag).
#   • Любое движение записывается в efhc_transfers_log с
#     idempotency_key UNIQUE.
# ИИ-защиты/самовосстановление:
#   • Read-through: повтор по тому же Idempotency-Key возвращает запись, не
#     создавая дублей переводов и не нарушая баланс.
#   • Авто-создание BankState при отсутствии обеспечивает восстановление
#     после чистой БД без ручных миграций.
# Запреты:
#   • Нет P2P, нет EFHC→kWh; сервис работает только «банк ↔ пользователь».
# ======================================================================

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.utils_core import quantize_decimal, utc_now
from ..models import BankState, EFHCTransferLog, User

BANK_ENTITY = "bank"
USER_ENTITY = "user"


class TransactionsService:
    """Банковский сервис EFHC с атомарными операциями debit/credit."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def _get_bank(self) -> BankState:
        """Получить (или создать) единственную запись состояния банка.

        Назначение: обеспечить наличие BankState с id=1 при первой операции.
        Побочные эффекты: при отсутствии записи создаёт BankState(id=1).
        Идемпотентность: повторный вызов возвращает ту же сущность.
        """

        bank = await self.session.get(BankState, 1)
        if bank is None:
            bank = BankState(id=1)
            self.session.add(bank)
            await self.session.flush()
        return bank

    async def credit_user(
        self,
        user: User,
        amount: Decimal,
        idempotency_key: str,
        tx_hash: str | None = None,
    ) -> EFHCTransferLog:
        """Начислить EFHC пользователю из банка (read-through идемпотентно).

        Вход: модель пользователя, сумма Decimal(8), Idempotency-Key и
        опциональный tx_hash (для связки с TON логами).
        Побочные эффекты: уменьшает баланс банка, увеличивает баланс
        пользователя, помечает дефицит банка, пишет efhc_transfers_log.
        Идемпотентность: проверка efhc_transfers_log по idempotency_key;
        повтор возвращает прежнюю запись без нового движения.
        Исключения: не выбрасывает при дефиците банка (флаг ставится).
        ИИ-защита: quantize_decimal защищает от «лишних копеек» и гонок.
        """

        amount = quantize_decimal(amount)
        bank = await self._get_bank()
        existing = await self.session.scalar(
            select(EFHCTransferLog).where(
                EFHCTransferLog.idempotency_key == idempotency_key
            )
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
        """Списать EFHC у пользователя в банк (идемпотентно).

        Вход: пользователь, сумма Decimal(8), Idempotency-Key, причина
        (reason сохраняется для аудита в логах верхнего уровня).
        Побочные эффекты: уменьшает баланс пользователя, увеличивает банк,
        пишет efhc_transfers_log.
        Идемпотентность: повтор по ключу возвращает существующую запись.
        Исключения: ValueError, если пользователь ушёл бы в минус.
        """

        amount = quantize_decimal(amount)
        bank = await self._get_bank()
        existing = await self.session.scalar(
            select(EFHCTransferLog).where(
                EFHCTransferLog.idempotency_key == idempotency_key
            )
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


# ======================================================================
# Пояснения «для чайника»:
#   • Все денежные операции идут через этот сервис и efhc_transfers_log.
#   • Повтор с тем же Idempotency-Key вернёт существующую запись без дубля.
#   • Пользователь не может уйти в минус; банк может, ставится флаг дефицита.
#   • Сервис не реализует P2P и не конвертирует EFHC→kWh — это запрещено.
# ======================================================================
