# -*- coding: utf-8 -*-
"""Enforce critical unique keys (idempotency/vip/etc.).

Назначение:
    • Убедиться, что ключевые ограничения уникальности применены в БД, даже
      если БД создавалась без Alembic или с частичным набором таблиц.
    • Критичные для идемпотентности поля получают UNIQUE через DO-блоки.

Канон/инварианты:
    • efhc_transfers_log.idempotency_key — read-through банк.
    • ton_inbox_logs.tx_hash — идемпотентность входящих TON.
    • shop_orders.idempotency_key/tx_hash — безопасное создание заказов.
    • users.telegram_id — однозначная идентификация пользователя.

ИИ-защита:
    • DO $$ ... $$ проверяет pg_constraint перед добавлением, избегая сбоев при
      повторном запуске или нестандартных состояниях БД.
"""

from __future__ import annotations

from typing import List, Tuple

from alembic import op
from sqlalchemy import text

from backend.app.core.config_core import get_settings
from backend.app.core.logging_core import get_logger

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | None = "0002"
branch_labels = None
depends_on = None

logger = get_logger(__name__)
settings = get_settings()
CORE = getattr(settings, "DB_SCHEMA_CORE", "efhc_core")

UNIQUE_CONSTRAINTS: List[Tuple[str, str, str]] = [
    ("efhc_transfers_log", "uq_efhc_transfers_idem_key", "idempotency_key"),
    ("ton_inbox_logs", "uq_ton_inbox_tx", "tx_hash"),
    ("shop_orders", "uq_shop_orders_idem_key", "idempotency_key"),
    ("shop_orders", "uq_shop_orders_tx_hash", "tx_hash"),
    ("users", "uq_users_telegram", "telegram_id"),
]


def _ensure_unique(table: str, constraint: str, columns: str) -> None:
    sql = f"""
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname = '{constraint}'
        ) THEN
            ALTER TABLE {CORE}.{table} ADD CONSTRAINT {constraint} UNIQUE ({columns});
        END IF;
    END $$;
    """
    op.execute(text(sql))


def upgrade() -> None:
    """Применить ключевые уникальные ограничения."""

    for table, constraint, columns in UNIQUE_CONSTRAINTS:
        logger.info(
            "Ensuring unique constraint", extra={"table": table, "constraint": constraint}
        )
        _ensure_unique(table, constraint, columns)


def downgrade() -> None:
    """Удалить добавленные уникальные ограничения."""

    bind = op.get_bind()
    for table, constraint, _ in UNIQUE_CONSTRAINTS:
        bind.execute(text(f"ALTER TABLE IF EXISTS {CORE}.{table} DROP CONSTRAINT IF EXISTS {constraint}"))


# ============================================================================
# Пояснения «для чайника»:
#   • Идемпотентность денег и входящих TON держится на этих UNIQUE.
#   • DO-блоки защищают от падений при повторном применении миграции.
#   • При откате ограничения удаляются, но данные остаются неизменны.
# ============================================================================
