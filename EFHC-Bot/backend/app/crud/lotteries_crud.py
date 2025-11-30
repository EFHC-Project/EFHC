# -*- coding: utf-8 -*-
# backend/app/crud/lotteries_crud.py
# =============================================================================
# Назначение:
#   • CRUD-операции пользовательского контура лотерей: чтение активных лотерей,
#     детализированный доступ к билетам и агрегатам без денежных побочных эффектов.
#   • Денежные операции (покупка билетов за EFHC) выполняет сервис через банк;
#     CRUD лишь создаёт/читает записи таблиц lotteries/lottery_tickets/... .
#
# Канон/инварианты:
#   • Статусы лотерей: draft|active|closed|completed (см. модель); CRUD не проводит
#     розыгрыш, а только фиксирует данные.
#   • Билеты уникальны в рамках лотереи (lottery_id, ticket_id) и нумеруются
#     последовательно; контроль последовательности остаётся на сервисах.
#   • Только cursor-based пагинация (created_at DESC, id DESC для лотерей;
#     ticket_id ASC для билетов). OFFSET запрещён.
#
# ИИ-защита/самовосстановление:
#   • create_ticket_if_absent() возвращает существующий билет при повторе ticket_id,
#     что позволяет безопасно повторять покупку с тем же idempotency_key на уровне сервиса.
#   • upsert_user_stat() работает через SELECT ... FOR UPDATE, предотвращая гонки при
#     обновлении счётчика билетов пользователя.
#
# Запреты:
#   • CRUD не проводит розыгрыш и не начисляет призы; только хранит факты.
#   • Никаких денежных списаний/начислений внутри слоя CRUD.
# =============================================================================
from __future__ import annotations

from datetime import datetime
from typing import Iterable

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models import (
    Lottery,
    LotteryNFTClaim,
    LotteryResult,
    LotteryTicket,
    LotteryUserStat,
)


class LotteriesCRUD:
    """CRUD-обёртка для лотерей/билетов без денежной логики."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_lottery(self, lottery_id: int) -> Lottery | None:
        """Получить лотерею по id."""

        return await self.session.get(Lottery, int(lottery_id))

    async def list_active_cursor(
        self,
        *,
        limit: int,
        cursor: tuple[datetime, int] | None = None,
    ) -> list[Lottery]:
        """Вернуть активные лотереи для витрины (status='active'), курсорно."""

        stmt: Select[Lottery] = (
            select(Lottery)
            .where(Lottery.status == "active")
            .order_by(Lottery.created_at.desc(), Lottery.id.desc())
            .limit(limit)
        )
        if cursor:
            ts, lid = cursor
            stmt = stmt.where((Lottery.created_at < ts) | ((Lottery.created_at == ts) & (Lottery.id < lid)))

        rows: Iterable[Lottery] = await self.session.scalars(stmt)
        return list(rows)

    async def get_max_ticket_id(self, lottery_id: int) -> int:
        """Возвратить максимальный ticket_id в лотерее (0, если билетов нет)."""

        stmt = select(func.max(LotteryTicket.ticket_id)).where(LotteryTicket.lottery_id == int(lottery_id))
        max_ticket = await self.session.scalar(stmt)
        return int(max_ticket or 0)

    async def create_ticket_if_absent(
        self,
        *,
        lottery_id: int,
        ticket_id: int,
        owner_telegram_id: int,
    ) -> LotteryTicket:
        """Идемпотентно вставить билет с фиксированным ticket_id."""

        stmt: Select[LotteryTicket] = select(LotteryTicket).where(
            LotteryTicket.lottery_id == int(lottery_id),
            LotteryTicket.ticket_id == int(ticket_id),
        )
        existing = await self.session.scalar(stmt)
        if existing:
            return existing

        ticket = LotteryTicket(
            lottery_id=int(lottery_id),
            ticket_id=int(ticket_id),
            owner_telegram_id=int(owner_telegram_id),
        )
        self.session.add(ticket)
        await self.session.flush()
        return ticket

    async def upsert_user_stat(
        self, *, lottery_id: int, owner_telegram_id: int, increment: int
    ) -> LotteryUserStat:
        """Увеличить счётчик билетов пользователя под блокировкой (FOR UPDATE)."""

        stmt: Select[LotteryUserStat] = (
            select(LotteryUserStat)
            .where(
                LotteryUserStat.lottery_id == int(lottery_id),
                LotteryUserStat.telegram_id == int(owner_telegram_id),
            )
            .with_for_update()
        )
        stat = await self.session.scalar(stmt)
        if stat is None:
            stat = LotteryUserStat(
                lottery_id=int(lottery_id),
                telegram_id=int(owner_telegram_id),
                tickets_count=increment,
            )
            self.session.add(stat)
        else:
            stat.tickets_count += int(increment)
        await self.session.flush()
        return stat

    async def set_result_if_absent(
        self,
        *,
        lottery_id: int,
        winning_ticket_id: int,
        winner_telegram_id: int,
        completed_at: datetime,
    ) -> LotteryResult:
        """
        Зафиксировать результат лотереи, если его ещё нет.

        Позволяет сервису повторно записать результат без дублей.
        """

        stmt: Select[LotteryResult] = select(LotteryResult).where(LotteryResult.lottery_id == int(lottery_id))
        existing = await self.session.scalar(stmt)
        if existing:
            return existing

        result = LotteryResult(
            lottery_id=int(lottery_id),
            winning_ticket_id=int(winning_ticket_id),
            winner_telegram_id=int(winner_telegram_id),
            completed_at=completed_at,
        )
        self.session.add(result)
        await self.session.flush()
        return result

    async def create_nft_claim_if_absent(
        self,
        *,
        lottery_id: int,
        winner_telegram_id: int,
        wallet_address: str | None,
        meta: dict | None,
    ) -> LotteryNFTClaim:
        """Идемпотентно создать заявку на NFT-приз (без автоперезаписи)."""

        stmt: Select[LotteryNFTClaim] = select(LotteryNFTClaim).where(
            LotteryNFTClaim.lottery_id == int(lottery_id),
            LotteryNFTClaim.winner_telegram_id == int(winner_telegram_id),
        )
        existing = await self.session.scalar(stmt)
        if existing:
            return existing

        claim = LotteryNFTClaim(
            lottery_id=int(lottery_id),
            winner_telegram_id=int(winner_telegram_id),
            wallet_address=wallet_address,
            meta=meta,
        )
        self.session.add(claim)
        await self.session.flush()
        return claim

    async def list_tickets_cursor(
        self,
        lottery_id: int,
        *,
        limit: int,
        cursor: int | None = None,
    ) -> list[LotteryTicket]:
        """Курсорная выборка билетов по lottery_id (ticket_id ASC)."""

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

    async def list_user_tickets_cursor(
        self,
        lottery_id: int,
        owner_telegram_id: int,
        *,
        limit: int,
        cursor: int | None = None,
    ) -> list[LotteryTicket]:
        """Курсорная выборка билетов пользователя (ticket_id ASC)."""

        stmt: Select[LotteryTicket] = (
            select(LotteryTicket)
            .where(
                LotteryTicket.lottery_id == int(lottery_id),
                LotteryTicket.owner_telegram_id == int(owner_telegram_id),
            )
            .order_by(LotteryTicket.ticket_id.asc())
            .limit(limit)
        )
        if cursor:
            stmt = stmt.where(LotteryTicket.ticket_id > int(cursor))

        rows: Iterable[LotteryTicket] = await self.session.scalars(stmt)
        return list(rows)


__all__ = ["LotteriesCRUD"]

# ============================================================================
# Пояснения «для чайника»:
#   • CRUD не продаёт билеты и не начисляет призы; он только пишет/читает строки.
#   • Идемпотентность достигается уникальностью (lottery_id,ticket_id) и методами
#     *_if_absent(), поэтому повторные вызовы безопасны.
#   • Пагинация по лотереям — курсор (created_at,id) DESC; по билетам — ticket_id ASC.
# ============================================================================
