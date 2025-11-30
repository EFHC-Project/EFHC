# -*- coding: utf-8 -*-
# backend/app/crud/admin/admin_bank_crud.py
# =============================================================================
# Назначение:
#   • Админский CRUD для конфигурации/кэша Банка EFHC: admin_bank_config и bank_state.
#   • Только чтение/обновление метаданных; никаких денежных операций и расчётов.
#
# Канон/инварианты:
#   • Источник истины о движениях — efhc_transfers_log (обслуживает transactions_service);
#     BankState — кэш для витрин, обновляется сервисом админки.
#   • Конфигурация банка уникальна по bank_user_id, активная запись одна.
#   • CRUD не меняет балансы пользователей и не записывает efhc_transfers_log.
#
# ИИ-защита/самовосстановление:
#   • upsert_config() безопасно применяет конфигурацию без создания дублей.
#   • lock_bank_state() берёт запись под FOR UPDATE, чтобы сервис мог атомарно
#     обновить кэш балансов (read-through), не нарушая идемпотентность.
#
# Запреты:
#   • Не выполнять денежных коррекций в этом слое; только сохранение кэша/конфигурации.
#   • Не дублировать банковскую бизнес-логику — её реализуют сервисы.
# =============================================================================
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.bank_models import AdminBankConfig, BankState


class AdminBankCRUD:
    """CRUD-обёртка для admin_bank_config и bank_state."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_config(self) -> AdminBankConfig | None:
        """Вернуть активную конфигурацию банка (берём первую по created_at)."""

        stmt: Select[AdminBankConfig] = select(AdminBankConfig).order_by(AdminBankConfig.created_at.asc()).limit(1)
        return await self.session.scalar(stmt)

    async def upsert_config(self, cfg: AdminBankConfig) -> AdminBankConfig:
        """Создать/обновить конфигурацию банка по bank_user_id."""

        stmt: Select[AdminBankConfig] = select(AdminBankConfig).where(AdminBankConfig.bank_user_id == cfg.bank_user_id)
        existing = await self.session.scalar(stmt)
        if existing:
            existing.ton_main_wallet = cfg.ton_main_wallet
            existing.is_enabled = cfg.is_enabled
            existing.meta = cfg.meta
            await self.session.flush()
            return existing
        self.session.add(cfg)
        await self.session.flush()
        return cfg

    async def lock_bank_state(self, bank_user_id: int) -> BankState | None:
        """Получить запись BankState под FOR UPDATE (для сервисного пересчёта)."""

        stmt: Select[BankState] = (
            select(BankState)
            .where(BankState.bank_user_id == int(bank_user_id))
            .with_for_update()
        )
        return await self.session.scalar(stmt)

    async def upsert_bank_state(
        self,
        *,
        bank_user_id: int,
        balance_main: str,
        balance_bonus: str,
        last_recalc_log_id: int | None,
        last_recalc_at: datetime | None,
        meta: dict | None = None,
    ) -> BankState:
        """
        Сохранить кэш-состояние банка (идемпотентный upsert).

        Значения балансов передаёт сервис после расчёта по журналу; CRUD их лишь фиксирует.
        """

        stmt: Select[BankState] = select(BankState).where(BankState.bank_user_id == int(bank_user_id)).with_for_update()
        state = await self.session.scalar(stmt)
        if state is None:
            state = BankState(
                bank_user_id=int(bank_user_id),
                balance_main=balance_main,
                balance_bonus=balance_bonus,
                last_recalc_log_id=last_recalc_log_id,
                last_recalc_at=last_recalc_at,
                meta=meta,
            )
            self.session.add(state)
        else:
            state.balance_main = balance_main
            state.balance_bonus = balance_bonus
            state.last_recalc_log_id = last_recalc_log_id
            state.last_recalc_at = last_recalc_at
            state.meta = meta
        await self.session.flush()
        return state


__all__ = ["AdminBankCRUD"]

# ============================================================================
# Пояснения «для чайника»:
#   • CRUD не двигает EFHC и не меняет efhc_transfers_log; он лишь хранит конфигурацию
#     и кэш балансов для админских витрин.
#   • Обновление bank_state должно вызываться только сервисом, который сверяется с журналом.
#   • Курсоры здесь не нужны: записи уникальны по bank_user_id.
# ============================================================================
