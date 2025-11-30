# -*- coding: utf-8 -*-
# backend/app/crud/admin/admin_withdrawals_crud.py
# =============================================================================
# Назначение:
#   • Админская выборка заявок на вывод/выплаты, отражённых в банковском журнале
#     efhc_transfers_log (reason='withdraw_request' и сопутствующие). Денежные
#     операции не выполняются.
#
# Канон/инварианты:
#   • Все денежные действия идут через банковский сервис; CRUD только читает лог.
#   • Пагинация — курсор (created_at DESC, id DESC); OFFSET запрещён.
#
# ИИ-защита/самовосстановление:
#   • list_withdrawals_cursor() можно безопасно повторять — он только читает.
#
# Запреты:
#   • CRUD не изменяет статусы выплат и не двигает балансы.
# =============================================================================
from __future__ import annotations

from datetime import datetime
from typing import Iterable

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.transactions_models import EfhcTransferLog


class AdminWithdrawalsCRUD:
    """Чтение записей выводов из efhc_transfers_log (без модификаций)."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def list_withdrawals_cursor(
        self,
        *,
        limit: int,
        cursor: tuple[datetime, int] | None = None,
    ) -> list[EfhcTransferLog]:
        """Курсорная выборка записей журнала по reason='withdraw_request'."""

        stmt: Select[EfhcTransferLog] = (
            select(EfhcTransferLog)
            .where(EfhcTransferLog.reason == "withdraw_request")
            .order_by(EfhcTransferLog.created_at.desc(), EfhcTransferLog.id.desc())
            .limit(limit)
        )
        if cursor:
            ts, tid = cursor
            stmt = stmt.where((EfhcTransferLog.created_at < ts) | ((EfhcTransferLog.created_at == ts) & (EfhcTransferLog.id < tid)))

        rows: Iterable[EfhcTransferLog] = await self.session.scalars(stmt)
        return list(rows)


__all__ = ["AdminWithdrawalsCRUD"]

# ============================================================================
# Пояснения «для чайника»:
#   • CRUD только читает журнал выводов; изменение статусов/начислений выполняют сервисы.
#   • Пагинация — курсор (created_at,id DESC); OFFSET не применяется.
# ============================================================================
