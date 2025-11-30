# -*- coding: utf-8 -*-
# backend/app/crud/panels_crud.py
# =============================================================================
# Назначение:
#   • CRUD-слой для таблицы panels (активные/архивные панели пользователя).
#   • Обеспечивает курсорные выборки, безопасное создание/архивацию под блокировкой
#     и не содержит денежной логики (покупки выполняет сервис через банк).
#
# Канон/инварианты:
#   • Денежные расчёты и лимиты (100 EFHC за панель, максимум 1000 активных) —
#     только на уровне сервисов; CRUD не двигает балансы и не проверяет лимиты.
#   • Используется только cursor-based пагинация (created_at DESC, id DESC);
#     OFFSET запрещён.
#   • Генерация энергии считается в сервисах/планировщике, CRUD лишь хранит поля
#     base_gen_per_sec / last_generated_at без перерасчётов.
#
# ИИ-защита/самовосстановление:
#   • Все записи читаются/обновляются под явной блокировкой, когда есть риск гонок
#     (FOR UPDATE), чтобы избежать двойного архивирования или создания «под гонку».
#   • create_panel() принимает готовые значения ставок/срока — сервис отвечает за
#     корректность; повторный вызов с теми же параметрами безопасен, т.к. CRUD не
#     производит внешних побочных эффектов.
#
# Запреты:
#   • Никаких денежных операций, пересчётов kWh или проверки VIP — это уровень сервисов.
#   • Не дублировать канонические константы GEN_PER_SEC_* внутри CRUD.
# =============================================================================
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Iterable

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models import Panel


class PanelsCRUD:
    """CRUD-обёртка для panels без денежных побочных эффектов."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_by_id(self, panel_id: int) -> Panel | None:
        """Получить панель по первичному ключу (без блокировки)."""

        return await self.session.get(Panel, int(panel_id))

    async def lock_by_id(self, panel_id: int) -> Panel | None:
        """Захватить панель под FOR UPDATE для безопасного обновления."""

        return await self.session.get(Panel, int(panel_id), with_for_update=True)

    async def create_panel(
        self,
        *,
        user_id: int,
        expires_at: datetime,
        base_gen_per_sec: Decimal,
        last_generated_at: datetime,
    ) -> Panel:
        """
        Создать запись активной панели.

        Денежные списания за покупку выполняет сервис; здесь только вставка строки.
        """

        panel = Panel(
            user_id=int(user_id),
            expires_at=expires_at,
            last_generated_at=last_generated_at,
            base_gen_per_sec=str(base_gen_per_sec),
            is_active=True,
            generated_kwh="0",
        )
        self.session.add(panel)
        await self.session.flush()
        return panel

    async def archive_panel(self, panel_id: int, *, archived_at: datetime) -> Panel | None:
        """
        Перевести панель в архив (is_active=false) под блокировкой.

        Не начисляет/не списывает ничего; используется сервисом архивирования.
        Повторный вызов безопасен и вернёт актуальное состояние.
        """

        panel = await self.lock_by_id(panel_id)
        if panel is None:
            return None
        if panel.is_active:
            panel.is_active = False
            panel.archived_at = archived_at
        await self.session.flush()
        return panel

    async def list_active_by_user_cursor(
        self,
        user_id: int,
        *,
        limit: int,
        cursor: tuple[datetime, int] | None = None,
    ) -> list[Panel]:
        """Вернуть активные панели пользователя с курсорной сортировкой."""

        stmt: Select[Panel] = (
            select(Panel)
            .where(Panel.user_id == int(user_id), Panel.is_active.is_(True))
            .order_by(Panel.created_at.desc(), Panel.id.desc())
            .limit(limit)
        )
        if cursor:
            ts, pid = cursor
            stmt = stmt.where((Panel.created_at < ts) | ((Panel.created_at == ts) & (Panel.id < pid)))

        rows: Iterable[Panel] = await self.session.scalars(stmt)
        return list(rows)

    async def list_user_history_cursor(
        self,
        user_id: int,
        *,
        limit: int,
        cursor: tuple[datetime, int] | None = None,
    ) -> list[Panel]:
        """Вернуть все панели пользователя (активные и архив) курсором."""

        stmt: Select[Panel] = (
            select(Panel)
            .where(Panel.user_id == int(user_id))
            .order_by(Panel.created_at.desc(), Panel.id.desc())
            .limit(limit)
        )
        if cursor:
            ts, pid = cursor
            stmt = stmt.where((Panel.created_at < ts) | ((Panel.created_at == ts) & (Panel.id < pid)))

        rows: Iterable[Panel] = await self.session.scalars(stmt)
        return list(rows)


__all__ = ["PanelsCRUD"]

# ============================================================================
# Пояснения «для чайника»:
#   • CRUD не знает ни про цену панели, ни про VIP — только про строки таблицы.
#   • Для списков используется курсор (created_at DESC, id DESC); OFFSET исключён.
#   • Архивация не списывает/не начисляет EFHC — это работа сервисов/планировщика.
# ============================================================================
