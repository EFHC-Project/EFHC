# -*- coding: utf-8 -*-
# backend/app/models/bank_models.py
# =============================================================================
# Назначение кода:
#   ORM-модели домена «Центральный Банк EFHC»:
#   • AdminBankConfig — конфигурация Банка EFHC (какому Telegram ID принадлежит
#     банк и какие TON-адреса считать банковскими).
#   • BankState       — кэш-состояние балансов Банка (main/bonus), ускоряющее
#     витрины админки.
#
# Канон/инварианты:
#   • Банк EFHC — единственный источник/приёмник движения EFHC
#     (все операции только «Банк ↔ Пользователь», P2P запрещён).
#   • Пользователи не могут уходить в минус (жёсткий запрет в user_models
#     и банковском сервисе), Банк МОЖЕТ быть отрицательным — это допустимо
#     и НЕ блокирует операции.
#   • Источник истины о движении — журнал efhc_transfers_log; BankState —
#     всего лишь кэш (read-through агрегация).
#   • Любые денежные суммы — Numeric(30, 8) в БД, работа с Decimal в сервисах,
#     округление вниз (ROUND_DOWN) выполняется ТОЛЬКО в сервисном слое.
#
# ИИ-защиты:
#   • BankState хранит контрольные поля last_recalc_log_id/last_recalc_at
#     для идемпотентного «догон-пересчёта» кэша из журнала операций.
#   • Индексы по (created_at, id) обеспечивают курсорную пагинацию и стабильные
#     витрины админки.
#   • meta(JSONB) — безопасное расширяемое поле (например, режим
#     processed_with_deficit, дополнительные флаги диагностики).
#
# Запреты:
#   • Модели НЕ выполняют никаких расчётов/пересчётов — этим занимается
#     банковский сервис (transactions_service / admin_bank_service).
#   • Никаких «суточных» ставок/логики в контексте Банка — только сухие
#     балансы и конфигурация.
# =============================================================================

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, Optional

from sqlalchemy import (
    BigInteger,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Numeric

from ..core.config_core import get_settings
from ..core.database_core import Base

settings = get_settings()
CORE_SCHEMA = settings.DB_SCHEMA_CORE  # например, "efhc_core"


# -----------------------------------------------------------------------------
# Конфигурация Банка EFHC
# -----------------------------------------------------------------------------
class AdminBankConfig(Base):
    """
    Конфигурация центрального Банка EFHC.

    Поля:
      • bank_user_id    — Telegram ID «сущности банка» (обычно ADMIN_BANK_TELEGRAM_ID).
      • ton_main_wallet — основной TON-адрес приёма средств.
      • is_enabled      — активна ли конфигурация.
      • meta            — дополнительные параметры (список разрешённых подписей,
                          режимы работы и т.д.).

    ИИ-защита:
      • Наличие uniq-ограничения по bank_user_id гарантирует, что в системе
        не окажется несколько активных конфигураций для одного и того же
        банковского аккаунта.
    """

    __tablename__ = "admin_bank_config"
    __table_args__ = (
        # В системе должна быть ровно одна актуальная запись для bank_user_id.
        UniqueConstraint("bank_user_id", name="uq_bankcfg_user"),
        {"schema": CORE_SCHEMA},
    )

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    bank_user_id: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        index=True,
    )
    ton_main_wallet: Mapped[Optional[str]] = mapped_column(
        String(128),
        nullable=True,
        index=True,
    )

    is_enabled: Mapped[bool] = mapped_column(
        nullable=False,
        default=True,
        server_default="true",
    )
    meta: Mapped[Optional[Dict[str, Any]]] = mapped_column(
        JSONB,
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    def __repr__(self) -> str:
        return (
            f"<AdminBankConfig id={self.id} "
            f"bank_uid={self.bank_user_id} enabled={self.is_enabled}>"
        )


Index(
    "ix_bankcfg_created_id",
    AdminBankConfig.created_at,
    AdminBankConfig.id,
    postgresql_using="btree",
    schema=CORE_SCHEMA,
)


# -----------------------------------------------------------------------------
# Кэш-состояние Банка EFHC (ускорение витрин админки)
# -----------------------------------------------------------------------------
class BankState(Base):
    """
    Кэш-агрегат текущих балансов Банка EFHC.

    Важно:
      • Источник истины — журнал efhc_transfers_log. Данная сущность — всего
        лишь производный кэш для быстрых экранов админки.
      • Значения balance_main/balance_bonus МОГУТ быть отрицательными — это
        норма по канону и не блокирует операции.
      • last_recalc_log_id/last_recalc_at помогают сервису «догонять» кэш после
        сбоев по read-through-схеме.

    Поля:
      • bank_user_id       — Telegram ID, соответствующий сущности банка.
      • balance_main       — текущий кэш-остаток основного баланса EFHC
                             (может быть < 0).
      • balance_bonus      — текущий кэш-остаток бонусного баланса EFHC
                             (может быть < 0).
      • last_recalc_log_id — последний обработанный id из efhc_transfers_log.
      • last_recalc_at     — время последнего успешного пересчёта кэша.
      • meta               — произвольные флаги/диагностика
                             (например, {"deficit": true}).

    ИИ-защита:
      • Сервис при расхождении кэша и журнала может полностью пересчитать
        BankState, используя last_recalc_log_id как точку догонки, сохраняя
        идемпотентность пересчётов.
    """

    __tablename__ = "bank_state"
    __table_args__ = (
        UniqueConstraint("bank_user_id", name="uq_bankstate_user"),
        # Балансы банка могут быть отрицательными — ЭТО не запрещаем CHECK-ом.
        {"schema": CORE_SCHEMA},
    )

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    bank_user_id: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        index=True,
    )

    # Кэш-балансы Банка (Numeric(30,8)); допускаем отрицательные значения.
    balance_main: Mapped[Decimal] = mapped_column(
        Numeric(30, 8),
        nullable=False,
        default=Decimal("0"),
        server_default="0",
    )
    balance_bonus: Mapped[Decimal] = mapped_column(
        Numeric(30, 8),
        nullable=False,
        default=Decimal("0"),
        server_default="0",
    )

    # Контрольные точки перерасчёта кэша из журнала.
    last_recalc_log_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        nullable=True,
        index=True,
    )
    last_recalc_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=True,
    )

    meta: Mapped[Optional[Dict[str, Any]]] = mapped_column(
        JSONB,
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    def __repr__(self) -> str:
        return (
            f"<BankState id={self.id} "
            f"main={self.balance_main} "
            f"bonus={self.balance_bonus} "
            f"last_log={self.last_recalc_log_id}>"
        )


Index(
    "ix_bankstate_created_id",
    BankState.created_at,
    BankState.id,
    postgresql_using="btree",
    schema=CORE_SCHEMA,
)
Index(
    "ix_bankstate_lastlog",
    BankState.last_recalc_log_id,
    postgresql_using="btree",
    schema=CORE_SCHEMA,
)


__all__ = [
    "AdminBankConfig",
    "BankState",
]
# =============================================================================
# Пояснения «для чайника»:
#   • Почему два объекта — AdminBankConfig и BankState?
#     AdminBankConfig указывает «кто такой банк» (какой tg-id и какой адрес TON
#     считать банковскими), а BankState хранит кэш-снимок балансов для мгновенной
#     отрисовки в админке. Источник истины при этом остаётся журнал
#     efhc_transfers_log.
#
#   • Что делать при конфликте значений BankState и журнала?
#     Банковский сервис обязан уметь в любой момент «пересчитать кэш с нуля»
#     (read-through агрегация). При сомнениях полагайтесь на журнал и пересчитывайте
#     кэш. BankState — вспомогательный слой, а не источник истины.
#
#   • Почему банк может быть «в минусе»?
#     Таков канон проекта: дефицит Банка отражает спрос и НЕ блокирует операции.
#     Пополнение — за счёт новых покупок, внешнего притока и/или ручной эмиссии
#     в админке. Пользователи при этом НИКОГДА не уходят в минус, контроль
#     реализуется в сервисах (ensure_user_non_negative_after, LockViolation).
# =============================================================================
