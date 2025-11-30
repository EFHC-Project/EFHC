# -*- coding: utf-8 -*-
# backend/app/crud/admin/admin_lotteries_crud.py
# =============================================================================
# Назначение:
#   • Админский CRUD для управления лотереями: создание/обновление карточек,
#     курсорные выборки билетов/результатов и заявки на NFT-приз. Денежные операции
#     отсутствуют — продажа билетов идёт через сервис/банк.
#
# Канон/инварианты:
#   • Статусы лотерей ограничены ENUM в модели; CRUD не меняет бизнес-правила,
#     а лишь сохраняет переданные сервисом значения.
#   • OFFSET не используется: курсоры по (created_at,id) для лотерей и ticket_id ASC
#     для билетов.
#   • Никаких P2P и EFHC→kWh; CRUD не трогает балансы.
#
# ИИ-защита/самовосстановление:
#   • upsert_lottery() позволяет повторно применять конфиг без создания дублей.
#   • lock_lottery() даёт FOR UPDATE для безопасной смены статуса/агрегатов.
#
# Запреты:
#   • CRUD не проводит розыгрыш и не выполняет денежных списаний/начислений.
#   • Не генерировать ticket_id — это делает сервис, чтобы сохранить последовательность.
# =============================================================================
from __future__ import annotations

from datetime import datetime
from typing import Iterable

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models import Lottery, LotteryNFTClaim, LotteryResult, LotteryTicket


class AdminLotteriesCRUD:
    """Админский CRUD для карточек лотерей и связанных сущностей."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def upsert_lottery(self, lottery: Lottery) -> Lottery:
        """Создать или обновить карточку лотереи по id."""

        if lottery.id:
            existing = await self.session.get(Lottery, int(lottery.id))
            if existing:
                existing.title = lottery.title
                existing.prize_type = lottery.prize_type
                existing.prize_value = lottery.prize_value
                existing.ticket_price = lottery.ticket_price
                existing.max_participants = lottery.max_participants
                existing.max_tickets_per_user = lottery.max_tickets_per_user
                existing.total_tickets = lottery.total_tickets
                existing.tickets_sold = lottery.tickets_sold
                existing.status = lottery.status
                existing.auto_draw = lottery.auto_draw
                existing.finished_at = lottery.finished_at
                await self.session.flush()
                return existing
        self.session.add(lottery)
        await self.session.flush()
        return lottery

    async def lock_lottery(self, lottery_id: int) -> Lottery | None:
        """Получить лотерею под FOR UPDATE для сервисных операций."""

        return await self.session.get(Lottery, int(lottery_id), with_for_update=True)

    async def list_lotteries_cursor(
        self,
        *,
        limit: int,
        cursor: tuple[datetime, int] | None = None,
        status: str | None = None,
    ) -> list[Lottery]:
        """Курсорная выборка лотерей для админ-витрины."""

        stmt: Select[Lottery] = (
            select(Lottery)
            .order_by(Lottery.created_at.desc(), Lottery.id.desc())
            .limit(limit)
        )
        if status:
            stmt = stmt.where(Lottery.status == status)
        if cursor:
            ts, lid = cursor
            stmt = stmt.where((Lottery.created_at < ts) | ((Lottery.created_at == ts) & (Lottery.id < lid)))

        rows: Iterable[Lottery] = await self.session.scalars(stmt)
        return list(rows)

    async def list_tickets_cursor(
        self,
        lottery_id: int,
        *,
        limit: int,
        cursor: int | None = None,
    ) -> list[LotteryTicket]:
        """Курсорная выдача билетов лотереи (ticket_id ASC)."""

        stmt: Select[LotteryTicket] = (
            select(LotteryTicket)
            .where(LotteryTicket.lottery_id == int(lottery_id))
            .order_by(LotteryTicket.ticket_id.asc())
            .limit(limit)
        )
        if cursor:
            stmt = stmt.where(LotteryTicket.ticket_id > int(cursor))
        rows: Iterable[LotteryTicket] = await self.session.scalars(stmt)
        return list(rows)

    async def get_result(self, lottery_id: int) -> LotteryResult | None:
        """Прочитать результат лотереи (если уже создан)."""

        stmt: Select[LotteryResult] = select(LotteryResult).where(LotteryResult.lottery_id == int(lottery_id))
        return await self.session.scalar(stmt)

    async def list_nft_claims_cursor(
        self,
        *,
        limit: int,
        cursor: int | None = None,
        status: str | None = None,
    ) -> list[LotteryNFTClaim]:
        """Курсорная выборка NFT-заявок (админ-модерация)."""

        stmt: Select[LotteryNFTClaim] = (
            select(LotteryNFTClaim)
            .order_by(LotteryNFTClaim.id.asc())
            .limit(limit)
        )
        if status:
            stmt = stmt.where(LotteryNFTClaim.status == status)
        if cursor:
            stmt = stmt.where(LotteryNFTClaim.id > int(cursor))

        rows: Iterable[LotteryNFTClaim] = await self.session.scalars(stmt)
        return list(rows)


__all__ = ["AdminLotteriesCRUD"]

# ============================================================================
# Пояснения «для чайника»:
#   • CRUD не продаёт билеты и не проводит розыгрыши — только управляет карточками
#     и читает/пишет связанные сущности.
#   • Все выборки — через курсоры (created_at,id DESC для лотерей, ticket_id ASC для билетов).
#   • Денежные операции выполняются сервисами через transactions_service.
# ============================================================================
