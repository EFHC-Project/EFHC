# Transaction model# -*- coding: utf-8 -*-
# backend/app/models/transactions_models.py
# =============================================================================
# Назначение кода:
#   ORM-модель единого банковского журнала EFHC:
#   • EfhcTransferLog — каждая запись фиксирует зеркальную операцию «пользователь ↔ банк».
#
# Канон/инварианты:
#   • Идемпотентность на уровне БД: idempotency_key UNIQUE (паттерн read-through).
#   • Денежная точность — Numeric(30,8); округление вниз выполняется в сервисе.
#   • Запрещены P2P-переводы: журнал хранит операции «банк ↔ пользователь», P2P не пишется.
#   • Пользователь не может уйти в минус (контролируется сервисами и CHECK в users);
#     банк может быть отрицательным (это не ошибка и не блокирует операции).
#
# ИИ-защиты:
#   • Курсорные индексы (created_at,id) для витрин и экспорта без OFFSET.
#   • Поле meta(JSONB) — безопасные расширения (например, привязка к TON tx_hash, SKU, claim_id).
#   • Жёсткие CHECKы на amount>=0 и белые списки для direction/balance_type.
#
# Запреты:
#   • Никакой бизнес-логики и пересчётов в модели — только хранение факта.
#   • Никаких «суточных» понятий; только точные значения сумм и метаданные.
# =============================================================================

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from ..core.config_core import get_settings
from ..core.database_core import Base  # единый declarative Base проекта

settings = get_settings()
CORE_SCHEMA = settings.DB_SCHEMA_CORE  # например, "efhc_core"


class EfhcTransferLog(Base):
    """
    Банковский журнал движения EFHC.

    Смысл записи:
      • direction     — 'credit' (начислили пользователю из банка) или 'debit' (списали у пользователя в банк).
      • balance_type  — 'main' | 'bonus' (какой пользовательский баланс затронут).
      • reason        — причина операции (например: 'exchange', 'panel_purchase', 'task_bonus',
                         'admin_mint', 'admin_burn', 'shop_auto', 'withdraw_request', ...).
      • idempotency_key — глобальный ключ идемпотентности (уникален в системе).
      • meta          — произвольные технические детали (tx_hash, SKU, TG_ID, трассировка и т.п.).

    Зеркальность:
      • Сервис фиксирует по одной записи на сторону пользователя. Сторона банка отражается отдельной
        записью с таким же idempotency_key, но с user_id = ADMIN_BANK_TELEGRAM_ID (из настроек).
        Таким образом, пара записей образует «двойную бухгалтерию».
    """

    __tablename__ = "efhc_transfers_log"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_efhc_transfers_idem_key"),
        CheckConstraint("amount >= 0", name="ck_efhc_transfers_amount_nonneg"),
        CheckConstraint("direction IN ('credit','debit')", name="ck_efhc_transfers_direction_enum"),
        CheckConstraint("balance_type IN ('main','bonus')", name="ck_efhc_transfers_baltype_enum"),
        {"schema": CORE_SCHEMA},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # «Сторона» операции: Telegram ID пользователя (или ADMIN_BANK_TELEGRAM_ID для записи банка)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)

    # Сумма EFHC (Decimal(30,8)); округление вниз выполняется на уровне сервисов
    amount: Mapped[str] = mapped_column(Numeric(30, 8), nullable=False)

    # Направление движения относительно user_id
    # credit — пользователю начислили из банка; debit — у пользователя списали в банк
    direction: Mapped[str] = mapped_column(String(8), nullable=False, index=True)

    # Какой пользовательский баланс затронут ('main' | 'bonus')
    balance_type: Mapped[str] = mapped_column(String(8), nullable=False, index=True)

    # Причина операции (короткий классификатор для витрин/отчётов)
    reason: Mapped[str] = mapped_column(String(32), nullable=False, index=True)

    # Глобальный ключ идемпотентности (один и тот же для пары «пользователь ↔ банк»)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False, index=True)

    # Вспомогательные данные (TON tx_hash, SKU, TG_ID, служебные флаги и прочее)
    meta: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB, nullable=True)

    # Таймстемпы
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return f"<EfhcTransferLog id={self.id} uid={self.user_id} {self.direction} {self.amount} {self.balance_type}>"


# Индексы под курсор/выборки без OFFSET и типовые отчёты
Index("ix_eftl_created_id", EfhcTransferLog.created_at, EfhcTransferLog.id,
      postgresql_using="btree", schema=CORE_SCHEMA)
Index("ix_eftl_user_created", EfhcTransferLog.user_id, EfhcTransferLog.created_at,
      postgresql_using="btree", schema=CORE_SCHEMA)
Index("ix_eftl_reason_created", EfhcTransferLog.reason, EfhcTransferLog.created_at,
      postgresql_using="btree", schema=CORE_SCHEMA)

class TonInboxLog(Base):
    """
    Журнал входящих TON-транзакций, которые наблюдает вотчер.

    Ключевые поля:
      • tx_hash         — уникальный идентификатор транзакции (UNIQUE).
      • from_address    — адрес отправителя TON.
      • to_address      — адрес проекта (кошелёк приёма).
      • amount          — сумма входящего платежа (в базовой валюте учёта TON; Numeric(30,8)).
      • memo            — исходная строка MEMO.
      • parsed_kind     — результат классификации MEMO ('EFHC_DEPOSIT' | 'SHOP_EFHC' | 'SHOP_NFT' | 'UNKNOWN').
      • parsed_tg_id    — извлечённый Telegram ID (если удалось).
      • parsed_sku      — извлечённый SKU (если удалось).
      • status          — статус обработки ('received', 'parsed', 'credited', 'paid_pending_manual',
                                             'finalized', 'error_retry', 'network_error_retry').
      • retries_count   — число повторных попыток обработки.
      • next_retry_at   — плановая дата следующей попытки (NULL → можно обрабатывать сразу).
      • processed_at    — когда операция успешно финализирована (credited/paid_pending_manual/finalized).
      • last_error      — текст последней ошибки для диагностики/самовосстановления.
      • meta            — произвольные данные (например, подробный результат парсинга).
    """

    __tablename__ = "ton_inbox_logs"
    __table_args__ = (
        UniqueConstraint("tx_hash", name="uq_ton_inbox_tx"),
        CheckConstraint("amount >= 0", name="ck_ton_inbox_amount_nonneg"),
        {"schema": CORE_SCHEMA},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    # TON-транзакция
    tx_hash: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    from_address: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    to_address: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)

    # Сумма транзакции (в эквиваленте базовой единицы TON по договорённости системы)
    amount: Mapped[str] = mapped_column(Numeric(30, 8), nullable=False, default="0", server_default="0")

    # MEMO и результат его разбора
    memo: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    parsed_kind: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, index=True)
    parsed_tg_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True, index=True)
    parsed_sku: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)

    # Статусная машина
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        index=True,
        default="received",
        server_default="received",
        doc="received|parsed|credited|paid_pending_manual|finalized|error_retry|network_error_retry",
    )
    retries_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    next_retry_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    processed_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True), nullable=True)

    # Диагностика/расширения
    last_error: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    meta: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB, nullable=True)

    # Таймстемпы создания/обновления записи
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return f"<TonInboxLog tx={self.tx_hash} status={self.status} kind={self.parsed_kind} tg={self.parsed_tg_id}>"



# Индексы для курсорной пагинации и типовых выборок
Index("ix_ton_inbox_created_id", TonInboxLog.created_at, TonInboxLog.id,
      postgresql_using="btree", schema=CORE_SCHEMA)
Index("ix_ton_inbox_status_nextretry", TonInboxLog.status, TonInboxLog.next_retry_at,
      postgresql_using="btree", schema=CORE_SCHEMA)
Index("ix_ton_inbox_parsed_kind", TonInboxLog.parsed_kind, postgresql_using="btree", schema=CORE_SCHEMA)


__all__ = [
    "TonInboxLog",
]
# =============================================================================
# Пояснения «для чайника»:
#   • Зачем уникальный tx_hash?
#     Чтобы одна и та же транзакция TON не была обработана дважды. Вотчер делает upsert по tx_hash
#     и использует статусную машину, поэтому повторный приход события не создаст дублей.
#
#   • Как происходит самовосстановление?
#     При сетевых/временных ошибках статус ставится в network_error_retry или error_retry,
#     увеличивается retries_count и назначается next_retry_at. Планировщик каждые 10 минут
#     подхватывает такие записи и пытается обработать повторно до финального статуса.
#
#   • Почему amount — Numeric(30,8)?
#     Мы используем Decimal(30,8) как общий формат денежных/квази-денежных величин в проекте.
#     Конвертация в EFHC/кВт·ч выполняется на уровне сервисов (строго 1:1 для kWh→EFHC).
# =============================================================================

__all__ = ["EfhcTransferLog", "TonInboxLog"]
