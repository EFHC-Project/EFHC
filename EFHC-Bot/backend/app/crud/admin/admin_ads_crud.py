# -*- coding: utf-8 -*-
# backend/app/crud/admin/admin_ads_crud.py
# =============================================================================
# Назначение:
#   • Админский CRUD для рекламных кампаний/показов (ads_campaigns, ads_impressions).
#   • Поддерживает курсорные списки, безопасное включение/выключение кампаний и
#     запись показов без влияния на балансы (рекламный бюджет — справочная величина).
#
# Канон/инварианты:
#   • Денежные движения EFHC не выполняются здесь; бюджет_efhc — учётное поле,
#     которое изменяет сервис/админка, но не двигает банк.
#   • Только cursor-based пагинация (created_at DESC, id DESC) для кампаний и
#     ASC по id для показов; OFFSET запрещён.
#
# ИИ-защита/самовосстановление:
#   • upsert_campaign() позволяет повторно применять seed-конфигурацию без дублей.
#   • create_impression() не требует внешних побочных эффектов и безопасен для повторов
#     в рамках одной кампании/пользователя, если вызывающий код контролирует идемпотентность.
#
# Запреты:
#   • CRUD не списывает бюджет и не начисляет награды за просмотр рекламы — это
#     ответственность сервисного уровня/банка.
# =============================================================================
from __future__ import annotations

from datetime import datetime
from typing import Iterable

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.ads_models import AdsCampaign, AdsImpression


class AdminAdsCRUD:
    """Админский CRUD для рекламных кампаний и фактов показов."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_campaign(self, campaign_id: int) -> AdsCampaign | None:
        """Прочитать кампанию по id."""

        return await self.session.get(AdsCampaign, int(campaign_id))

    async def upsert_campaign(self, campaign: AdsCampaign) -> AdsCampaign:
        """Создать или обновить кампанию по id (идемпотентно для seed-скриптов)."""

        if campaign.id:
            existing = await self.session.get(AdsCampaign, int(campaign.id))
            if existing:
                existing.title = campaign.title
                existing.active = campaign.active
                existing.budget_efhc = campaign.budget_efhc
                await self.session.flush()
                return existing
        self.session.add(campaign)
        await self.session.flush()
        return campaign

    async def set_active(self, campaign_id: int, *, active: bool) -> AdsCampaign | None:
        """Включить/выключить кампанию под блокировкой."""

        campaign = await self.session.get(AdsCampaign, int(campaign_id), with_for_update=True)
        if campaign is None:
            return None
        campaign.active = active
        await self.session.flush()
        return campaign

    async def list_campaigns_cursor(
        self,
        *,
        limit: int,
        cursor: tuple[datetime, int] | None = None,
        only_active: bool | None = None,
    ) -> list[AdsCampaign]:
        """Курсорная выборка кампаний для админ-витрины."""

        stmt: Select[AdsCampaign] = (
            select(AdsCampaign)
            .order_by(AdsCampaign.created_at.desc(), AdsCampaign.id.desc())
            .limit(limit)
        )
        if only_active is True:
            stmt = stmt.where(AdsCampaign.active.is_(True))
        elif only_active is False:
            stmt = stmt.where(AdsCampaign.active.is_(False))
        if cursor:
            ts, cid = cursor
            stmt = stmt.where((AdsCampaign.created_at < ts) | ((AdsCampaign.created_at == ts) & (AdsCampaign.id < cid)))

        rows: Iterable[AdsCampaign] = await self.session.scalars(stmt)
        return list(rows)

    async def create_impression(
        self,
        *,
        campaign_id: int,
        user_id: int,
        created_at: datetime | None = None,
    ) -> AdsImpression:
        """Записать факт показа рекламы (без денежных списаний)."""

        impression = AdsImpression(
            campaign_id=int(campaign_id),
            user_id=int(user_id),
            created_at=created_at or datetime.utcnow(),
        )
        self.session.add(impression)
        await self.session.flush()
        return impression

    async def list_impressions_cursor(
        self,
        *,
        campaign_id: int | None = None,
        user_id: int | None = None,
        limit: int,
        cursor: int | None = None,
    ) -> list[AdsImpression]:
        """Курсорный список показов (ASC по id)."""

        stmt: Select[AdsImpression] = select(AdsImpression).order_by(AdsImpression.id.asc()).limit(limit)
        if campaign_id:
            stmt = stmt.where(AdsImpression.campaign_id == int(campaign_id))
        if user_id:
            stmt = stmt.where(AdsImpression.user_id == int(user_id))
        if cursor:
            stmt = stmt.where(AdsImpression.id > int(cursor))

        rows: Iterable[AdsImpression] = await self.session.scalars(stmt)
        return list(rows)


__all__ = ["AdminAdsCRUD"]

# ============================================================================
# Пояснения «для чайника»:
#   • CRUD не списывает рекламный бюджет и не начисляет вознаграждения — только учёт записей.
#   • Для списков кампаний/показов используются курсоры; OFFSET не применяется.
#   • Активность кампании переключается set_active(), расчёт бюджета выполняют сервисы.
# ============================================================================
