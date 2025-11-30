# -*- coding: utf-8 -*-
# backend/app/crud/referrals_crud.py
# =============================================================================
# Назначение:
#   • CRUD-операции по таблице referral_links: создание связи «пригласивший →
#     приглашённый», выборки по курсору и маркировка «активных» рефералов.
#   • Денежных действий нет: начисления бонусов выполняются только сервисом через банк.
#
# Канон/инварианты:
#   • Один приглашённый может иметь только одного родителя (UNIQUE referee_id).
#   • Активность реферала определяется наличием activated_at (первая покупка панели).
#   • Только cursor-based пагинация (created_at DESC, id DESC), OFFSET запрещён.
#
# ИИ-защита/самовосстановление:
#   • create_if_absent() делает read-through по referee_id, исключая дубли даже при
#     повторной регистрации с тем же кодом.
#   • mark_activated() работает под блокировкой строки, чтобы не потерять метку
#     активации при гонке нескольких процессов.
#
# Запреты:
#   • CRUD не начисляет реферальных бонусов и не меняет балансы.
#   • Никаких расчётов уровней/деревьев — только хранение связей.
# =============================================================================
from __future__ import annotations

from datetime import datetime
from typing import Iterable

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models import ReferralLink


class ReferralsCRUD:
    """CRUD-обёртка для referral_links без денежной логики."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_by_referee(self, referee_id: int) -> ReferralLink | None:
        """Найти запись по приглашённому (referee_id)."""

        stmt: Select[ReferralLink] = select(ReferralLink).where(ReferralLink.referee_id == int(referee_id))
        return await self.session.scalar(stmt)

    async def create_if_absent(
        self,
        *,
        referrer_id: int,
        referee_id: int,
        campaign: str | None = None,
    ) -> ReferralLink:
        """
        Идемпотентно создать связь «пригласивший → приглашённый».

        При повторе возвращает существующую запись без дублей.
        """

        existing = await self.get_by_referee(referee_id)
        if existing:
            return existing

        link = ReferralLink(referrer_id=int(referrer_id), referee_id=int(referee_id), campaign=campaign)
        self.session.add(link)
        await self.session.flush()
        return link

    async def mark_activated(self, referee_id: int, *, activated_at: datetime) -> ReferralLink | None:
        """Проставить activated_at для приглашённого (под блокировкой)."""

        stmt: Select[ReferralLink] = (
            select(ReferralLink)
            .where(ReferralLink.referee_id == int(referee_id))
            .with_for_update()
        )
        link = await self.session.scalar(stmt)
        if link is None:
            return None
        if link.activated_at is None:
            link.activated_at = activated_at
            await self.session.flush()
        return link

    async def list_by_referrer_cursor(
        self,
        referrer_id: int,
        *,
        limit: int,
        cursor: tuple[datetime, int] | None = None,
        only_active: bool | None = None,
    ) -> list[ReferralLink]:
        """Курсорный список рефералов по пригласившему (фильтр по активности опционален)."""

        stmt: Select[ReferralLink] = (
            select(ReferralLink)
            .where(ReferralLink.referrer_id == int(referrer_id))
            .order_by(ReferralLink.created_at.desc(), ReferralLink.id.desc())
            .limit(limit)
        )
        if only_active is True:
            stmt = stmt.where(ReferralLink.activated_at.is_not(None))
        elif only_active is False:
            stmt = stmt.where(ReferralLink.activated_at.is_(None))
        if cursor:
            ts, rid = cursor
            stmt = stmt.where((ReferralLink.created_at < ts) | ((ReferralLink.created_at == ts) & (ReferralLink.id < rid)))

        rows: Iterable[ReferralLink] = await self.session.scalars(stmt)
        return list(rows)

    async def list_all_cursor(
        self,
        *,
        limit: int,
        cursor: tuple[datetime, int] | None = None,
    ) -> list[ReferralLink]:
        """Курсорная выборка всех связей (админ-витрина)."""

        stmt: Select[ReferralLink] = (
            select(ReferralLink)
            .order_by(ReferralLink.created_at.desc(), ReferralLink.id.desc())
            .limit(limit)
        )
        if cursor:
            ts, rid = cursor
            stmt = stmt.where((ReferralLink.created_at < ts) | ((ReferralLink.created_at == ts) & (ReferralLink.id < rid)))

        rows: Iterable[ReferralLink] = await self.session.scalars(stmt)
        return list(rows)


__all__ = ["ReferralsCRUD"]

# ============================================================================
# Пояснения «для чайника»:
#   • CRUD не начисляет бонусы и не меняет балансы; только структура связей.
#   • Активность реферала = наличие activated_at, проставляется сервисом панелей.
#   • Все списки — только через курсоры (created_at DESC, id DESC).
# ============================================================================
