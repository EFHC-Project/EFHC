# -*- coding: utf-8 -*-
# backend/app/crud/admin/admin_stats_crud.py
# =============================================================================
# Назначение:
#   • Админский слой для агрегированных выборок/метрик без бизнес-логики: курсорные
#     списки по банковскому журналу/TON-входящим для отчётов. Денежные операции
#     не выполняются.
#
# Канон/инварианты:
#   • Источник данных — существующие таблицы (efhc_transfers_log, ton_inbox_logs).
#     CRUD ничего не подсчитывает сам, только отдаёт выборки.
#   • Только курсорная пагинация (created_at DESC, id DESC); OFFSET не применяется.
#
# ИИ-защита/самовосстановление:
#   • Методы list_* используют простые SELECT без побочных эффектов, безопасные для повторов.
#
# Запреты:
#   • Не выполнять агрегаты/суммирование в CRUD; это задача сервисов/аналитики.
#   • Не изменять записи — только чтение.
# =============================================================================
from __future__ import annotations

from datetime import datetime
from typing import Iterable

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.transactions_models import EfhcTransferLog, TonInboxLog


class AdminStatsCRUD:
    """Чтение журналов для админских отчётов (без модификаций)."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def list_transfers_cursor(
        self,
        *,
        limit: int,
        cursor: tuple[datetime, int] | None = None,
        reason: str | None = None,
    ) -> list[EfhcTransferLog]:
        """Курсорная выборка efhc_transfers_log для отчётов."""

        stmt: Select[EfhcTransferLog] = (
            select(EfhcTransferLog)
            .order_by(EfhcTransferLog.created_at.desc(), EfhcTransferLog.id.desc())
            .limit(limit)
        )
        if reason:
            stmt = stmt.where(EfhcTransferLog.reason == reason)
        if cursor:
            ts, tid = cursor
            stmt = stmt.where((EfhcTransferLog.created_at < ts) | ((EfhcTransferLog.created_at == ts) & (EfhcTransferLog.id < tid)))

        rows: Iterable[EfhcTransferLog] = await self.session.scalars(stmt)
        return list(rows)

    async def list_ton_inbox_cursor(
        self,
        *,
        limit: int,
        cursor: tuple[datetime, int] | None = None,
        status: str | None = None,
    ) -> list[TonInboxLog]:
        """Курсорная выборка входящих TON-логов (для админ-диагностики)."""

        stmt: Select[TonInboxLog] = (
            select(TonInboxLog)
            .order_by(TonInboxLog.created_at.desc(), TonInboxLog.id.desc())
            .limit(limit)
        )
        if status:
            stmt = stmt.where(TonInboxLog.status == status)
        if cursor:
            ts, lid = cursor
            stmt = stmt.where((TonInboxLog.created_at < ts) | ((TonInboxLog.created_at == ts) & (TonInboxLog.id < lid)))

        rows: Iterable[TonInboxLog] = await self.session.scalars(stmt)
        return list(rows)


__all__ = ["AdminStatsCRUD"]

# ============================================================================
# Пояснения «для чайника»:
#   • CRUD только читает журналы для отчётности; никаких модификаций данных.
#   • Все выборки используют курсоры (created_at DESC, id DESC); OFFSET отсутствует.
#   • Денежные операции выполняются сервисами/банком, не здесь.
# ============================================================================
