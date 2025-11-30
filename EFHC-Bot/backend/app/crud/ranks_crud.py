# -*- coding: utf-8 -*-
# backend/app/crud/ranks_crud.py
# =============================================================================
# Назначение:
#   • CRUD-слой для рейтинговых снимков (rating_snapshots): курсорные выборки
#     и безопасное добавление новых записей без изменения бизнес-логики рейтинга.
#
# Канон/инварианты:
#   • Снимки генерируются сервисом раз в тик планировщика (10 минут) и не
#     пересчитываются в CRUD.
#   • Только cursor-based пагинация (created_at DESC, id DESC); OFFSET запрещён.
#   • Денежных операций нет; CRUD не трогает балансы.
#
# ИИ-защита/самовосстановление:
#   • add_snapshot() — простая вставка; повтор с теми же данными безопасен для БД,
#     так как уникальность определяется сервисом.
#
# Запреты:
#   • Не реализовывать пересчёты/агрегации рейтинга в CRUD.
#   • Не дублировать бизнес-константы (скорость генерации и т.п.).
# =============================================================================
from __future__ import annotations

from datetime import datetime
from typing import Iterable

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models import RatingSnapshot


class RanksCRUD:
    """CRUD-обёртка для rating_snapshots без расчётной логики."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def add_snapshot(self, snapshot: RatingSnapshot) -> RatingSnapshot:
        """Сохранить новый снимок рейтинга (commit — на вызывающей стороне)."""

        self.session.add(snapshot)
        await self.session.flush()
        return snapshot

    async def list_cursor(
        self,
        *,
        limit: int,
        cursor: tuple[datetime, int] | None = None,
    ) -> list[RatingSnapshot]:
        """Курсорная выборка снимков рейтинга (created_at DESC, id DESC)."""

        stmt: Select[RatingSnapshot] = (
            select(RatingSnapshot)
            .order_by(RatingSnapshot.created_at.desc(), RatingSnapshot.id.desc())
            .limit(limit)
        )
        if cursor:
            ts, sid = cursor
            stmt = stmt.where((RatingSnapshot.created_at < ts) | ((RatingSnapshot.created_at == ts) & (RatingSnapshot.id < sid)))

        rows: Iterable[RatingSnapshot] = await self.session.scalars(stmt)
        return list(rows)


__all__ = ["RanksCRUD"]

# ============================================================================
# Пояснения «для чайника»:
#   • CRUD не считает рейтинг и не обновляет балансы — только хранит снимки.
#   • Для списков используется курсор (created_at DESC, id DESC), OFFSET не применяется.
# ============================================================================
