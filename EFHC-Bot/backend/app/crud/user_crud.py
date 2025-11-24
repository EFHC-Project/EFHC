"""User CRUD with cursor helpers and idempotent upsert by telegram_id.

======================================================================
Назначение:
    • Безопасный доступ к таблице users: поиск, создание без дублей,
      обновление кошелька/VIP-флага, курсорные выборки.
    • Денежные балансы здесь не изменяются; операции с EFHC выполняются
      только через банковский сервис.

Канон/инварианты:
    • Пользователь не может уйти в минус — проверяется на уровне сервисов,
      CRUD не трогает балансы.
    • P2P, обратная конверсия EFHC→kWh и денежные операции отсутствуют.
    • Только cursor-based пагинация (created_at DESC, id DESC) без OFFSET.

ИИ-защита/самовосстановление:
    • create_if_absent() делает read-through по telegram_id, избегая дублей.
    • lock_for_update() предотвращает гонки при обновлении кошелька/VIP.

Запреты:
    • Не обновлять балансы в CRUD; только сервисы двигают деньги.
======================================================================
"""

from __future__ import annotations

from datetime import datetime
from typing import Iterable

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.logging_core import get_logger
from backend.app.models import User

logger = get_logger(__name__)


class UserCRUD:
    """CRUD-обёртка для users без денежных побочных эффектов."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_by_id(self, user_id: int) -> User | None:
        """Получить пользователя по первичному ключу."""

        return await self.session.get(User, int(user_id))

    async def get_by_telegram(self, telegram_id: int) -> User | None:
        """Найти пользователя по Telegram ID (уникальное поле)."""

        stmt: Select[User] = select(User).where(User.telegram_id == telegram_id)
        return await self.session.scalar(stmt)

    async def create_if_absent(
        self, telegram_id: int, ton_wallet: str | None = None
    ) -> User:
        """Идемпотентно создать пользователя по telegram_id.

        При повторном вызове возвращает существующую запись без дублей.
        Денежные балансы не меняет, commit выполняет вызывающий код.
        """

        existing = await self.get_by_telegram(telegram_id)
        if existing:
            return existing
        user = User(telegram_id=telegram_id, ton_wallet=ton_wallet)
        self.session.add(user)
        await self.session.flush()
        return user

    async def lock_for_update(self, user_id: int) -> User | None:
        """Получить пользователя под FOR UPDATE (для безопасных правок).

        Денежные действия здесь не выполняются, только блокировка строки.
        """

        return await self.session.get(User, int(user_id), with_for_update=True)

    async def update_wallet(
        self, user_id: int, *, ton_wallet: str | None = None
    ) -> User | None:
        """Обновить кошелёк пользователя под блокировкой."""

        user = await self.lock_for_update(user_id)
        if user is None:
            return None
        if ton_wallet:
            user.ton_wallet = ton_wallet
        await self.session.flush()
        return user

    async def set_vip(self, user_id: int, is_vip: bool) -> User | None:
        """Проставить VIP-флаг (кошелёк с NFT проверяет сервис)."""

        user = await self.lock_for_update(user_id)
        if user is None:
            return None
        user.is_vip = is_vip
        await self.session.flush()
        return user

    async def list_cursor(
        self,
        *,
        limit: int,
        cursor: tuple[datetime, int] | None = None,
    ) -> list[User]:
        """Курсорная выборка пользователей для админских списков."""

        stmt: Select[User] = (
            select(User)
            .order_by(User.created_at.desc(), User.id.desc())
            .limit(limit)
        )
        if cursor:
            ts, uid = cursor
            stmt = stmt.where((User.created_at < ts) | ((User.created_at == ts) & (User.id < uid)))
        rows: Iterable[User] = await self.session.scalars(stmt)
        return list(rows)


__all__ = ["UserCRUD"]

# ======================================================================
# Пояснения «для чайника»:
#   • CRUD не трогает деньги и не меняет балансы — только читает/создаёт
#     записи users и обновляет кошелёк/VIP-флаг под блокировкой.
#   • Дубли по telegram_id исключены за счёт create_if_absent (read-through).
#   • Пагинация только курсорная: created_at DESC, id DESC.
# ======================================================================
