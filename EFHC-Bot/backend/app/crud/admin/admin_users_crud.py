# -*- coding: utf-8 -*-
# backend/app/crud/admin/admin_users_crud.py
# =============================================================================
# Назначение:
#   • Админский CRUD для пользователей: курсорные выборки, блокировка строки для
#     безопасных правок кошелька/VIP-флага без денежных операций.
#
# Канон/инварианты:
#   • Балансы пользователей не меняются в CRUD; любые движения EFHC выполняются
#     через банковский сервис. OFFSET запрещён (только курсоры).
#   • VIP-флаг определяется наличием NFT (проверяет сервис); CRUD лишь сохраняет
#     boolean по запросу сервиса/админки.
#
# ИИ-защита/самовосстановление:
#   • lock_user() берёт FOR UPDATE, предотвращая гонки при обновлении кошелька/VIP.
#
# Запреты:
#   • Не трогать денежные поля main_balance/bonus_balance в админском CRUD.
#   • Не создавать пользователей вручную, обходя пользовательский поток без нужды.
# =============================================================================
from __future__ import annotations

from datetime import datetime
from typing import Iterable

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models import User


class AdminUsersCRUD:
    """Админский CRUD для безопасных правок пользователей (без денег)."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def lock_user(self, user_id: int) -> User | None:
        """Получить пользователя под FOR UPDATE."""

        return await self.session.get(User, int(user_id), with_for_update=True)

    async def set_wallet_and_vip(
        self,
        user_id: int,
        *,
        ton_wallet: str | None = None,
        is_vip: bool | None = None,
    ) -> User | None:
        """Обновить кошелёк/VIP флаг (денежные поля не трогаем)."""

        user = await self.lock_user(user_id)
        if user is None:
            return None
        if ton_wallet is not None:
            user.ton_wallet = ton_wallet
        if is_vip is not None:
            user.is_vip = is_vip
        await self.session.flush()
        return user

    async def list_users_cursor(
        self,
        *,
        limit: int,
        cursor: tuple[datetime, int] | None = None,
        is_vip: bool | None = None,
    ) -> list[User]:
        """Курсорная выборка пользователей для админ-витрины."""

        stmt: Select[User] = (
            select(User)
            .order_by(User.created_at.desc(), User.id.desc())
            .limit(limit)
        )
        if is_vip is True:
            stmt = stmt.where(User.is_vip.is_(True))
        elif is_vip is False:
            stmt = stmt.where(User.is_vip.is_(False))
        if cursor:
            ts, uid = cursor
            stmt = stmt.where((User.created_at < ts) | ((User.created_at == ts) & (User.id < uid)))

        rows: Iterable[User] = await self.session.scalars(stmt)
        return list(rows)


__all__ = ["AdminUsersCRUD"]

# ============================================================================
# Пояснения «для чайника»:
#   • CRUD не изменяет балансы — только кошелёк/VIP флаг под блокировкой.
#   • Все выборки используют курсоры; OFFSET запрещён.
#   • Денежные операции выполняются сервисами через банк.
# ============================================================================
