# -*- coding: utf-8 -*-
# backend/app/crud/admin/admin_referrals_crud.py
# =============================================================================
# Назначение:
#   • Админский CRUD для реферальных связей: курсорные списки, пометка активности,
#     фильтры по кампании/статусу. Денежных действий нет.
#
# Канон/инварианты:
#   • Один приглашённый — один реферер (UNIQUE referee_id), активность через activated_at.
#   • OFFSET запрещён: курсоры по (created_at,id) DESC.
#   • CRUD не начисляет бонусы и не двигает EFHC.
#
# ИИ-защита/самовосстановление:
#   • mark_activated() работает под FOR UPDATE, чтобы не потерять дату активации.
#
# Запреты:
#   • Не создавать связи в админке в обход пользовательского потока — используем сервисы/seed.
# =============================================================================
from __future__ import annotations

from datetime import datetime
from typing import Iterable

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models import ReferralLink


class AdminReferralsCRUD:
    """Админский CRUD для просмотра/пометки реферальных связей."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def mark_activated(self, link_id: int, *, activated_at: datetime) -> ReferralLink | None:
        """Проставить activated_at под блокировкой."""

        link = await self.session.get(ReferralLink, int(link_id), with_for_update=True)
        if link is None:
            return None
        if link.activated_at is None:
            link.activated_at = activated_at
            await self.session.flush()
        return link

    async def list_links_cursor(
        self,
        *,
        limit: int,
        cursor: tuple[datetime, int] | None = None,
        only_active: bool | None = None,
        campaign: str | None = None,
    ) -> list[ReferralLink]:
        """Курсорная выборка связей с фильтрами по активности/кампании."""

        stmt: Select[ReferralLink] = (
            select(ReferralLink)
            .order_by(ReferralLink.created_at.desc(), ReferralLink.id.desc())
            .limit(limit)
        )
        if only_active is True:
            stmt = stmt.where(ReferralLink.activated_at.is_not(None))
        elif only_active is False:
            stmt = stmt.where(ReferralLink.activated_at.is_(None))
        if campaign:
            stmt = stmt.where(ReferralLink.campaign == campaign)
        if cursor:
            ts, rid = cursor
            stmt = stmt.where((ReferralLink.created_at < ts) | ((ReferralLink.created_at == ts) & (ReferralLink.id < rid)))

        rows: Iterable[ReferralLink] = await self.session.scalars(stmt)
        return list(rows)


__all__ = ["AdminReferralsCRUD"]

# ============================================================================
# Пояснения «для чайника»:
#   • CRUD не начисляет бонусы — только чтение/пометка activated_at.
#   • Все выборки через курсоры (created_at DESC, id DESC).
#   • Создание связей должно идти через пользовательский поток/сервисы, а не вручную.
# ============================================================================
