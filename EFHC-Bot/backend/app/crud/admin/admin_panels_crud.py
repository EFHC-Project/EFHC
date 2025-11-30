# -*- coding: utf-8 -*-
# backend/app/crud/admin/admin_panels_crud.py
# =============================================================================
# Назначение:
#   • Админский CRUD для панели: курсорные выборки, безопасная архивация,
#     обновление метаданных без денежных операций.
#
# Канон/инварианты:
#   • Денежные списания за панели выполняются сервисами через банк; CRUD не трогает
#     балансы и не проверяет лимиты.
#   • OFFSET не используется: курсоры по (created_at,id) DESC.
#
# ИИ-защита/самовосстановление:
#   • lock_panel() берёт FOR UPDATE, чтобы админские правки (архив/метаданные) не
#     конфликтовали с сервисами/планировщиком.
#
# Запреты:
#   • Не изменять base_gen_per_sec/генерацию в CRUD — это зона сервисов.
#   • Не создавать новые панели в админском CRUD, чтобы не обходить банковский слой.
# =============================================================================
from __future__ import annotations

from datetime import datetime
from typing import Iterable

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models import Panel


class AdminPanelsCRUD:
    """Админский CRUD для панелей (без денежной логики)."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def lock_panel(self, panel_id: int) -> Panel | None:
        """Получить панель под FOR UPDATE."""

        return await self.session.get(Panel, int(panel_id), with_for_update=True)

    async def archive_panel(self, panel_id: int, *, archived_at: datetime) -> Panel | None:
        """Принудительно перевести панель в архив (без денежных эффектов)."""

        panel = await self.lock_panel(panel_id)
        if panel is None:
            return None
        panel.is_active = False
        panel.archived_at = archived_at
        await self.session.flush()
        return panel

    async def update_meta(
        self, panel_id: int, *, title: str | None = None, meta: dict | None = None
    ) -> Panel | None:
        """Обновить человеко-читаемое название/мета-информацию панели."""

        panel = await self.lock_panel(panel_id)
        if panel is None:
            return None
        if title is not None:
            panel.title = title
        if meta is not None:
            panel.meta = meta
        await self.session.flush()
        return panel

    async def list_panels_cursor(
        self,
        *,
        limit: int,
        cursor: tuple[datetime, int] | None = None,
        is_active: bool | None = None,
        user_id: int | None = None,
    ) -> list[Panel]:
        """Курсорная выборка панелей для админской витрины."""

        stmt: Select[Panel] = (
            select(Panel)
            .order_by(Panel.created_at.desc(), Panel.id.desc())
            .limit(limit)
        )
        if is_active is True:
            stmt = stmt.where(Panel.is_active.is_(True))
        elif is_active is False:
            stmt = stmt.where(Panel.is_active.is_(False))
        if user_id:
            stmt = stmt.where(Panel.user_id == int(user_id))
        if cursor:
            ts, pid = cursor
            stmt = stmt.where((Panel.created_at < ts) | ((Panel.created_at == ts) & (Panel.id < pid)))

        rows: Iterable[Panel] = await self.session.scalars(stmt)
        return list(rows)


__all__ = ["AdminPanelsCRUD"]

# ============================================================================
# Пояснения «для чайника»:
#   • CRUD не создаёт панели и не двигает деньги; он лишь позволяет админам
#     безопасно архивировать/переименовывать записи.
#   • Все выборки используют курсоры, чтобы избежать OFFSET и гонок.
#   • Денежные действия по панелям выполняют сервисы через transactions_service.
# ============================================================================
