# -*- coding: utf-8 -*-
"""Cursor/pagination indexes (canon v2.8).

Назначение:
    • Добавить/обновить индексы под курсорную пагинацию и быстрые выборки.
    • Индексы создаются с IF NOT EXISTS для безопасного повторного запуска.

Канон/инварианты:
    • Только DDL: деньги не двигаются, балансы не меняются.
    • Основные таблицы витрин получают btree(created_at, id).

ИИ-защита:
    • IF NOT EXISTS исключает падения при повторе или ручных правках.
"""

from __future__ import annotations

from typing import List

from alembic import op
from sqlalchemy import text

from backend.app.core.config_core import get_settings
from backend.app.core.logging_core import get_logger

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | None = "0001"
branch_labels = None
depends_on = None

logger = get_logger(__name__)
settings = get_settings()
CORE = getattr(settings, "DB_SCHEMA_CORE", "efhc_core")

INDEX_SQL: List[str] = [
    f"CREATE INDEX IF NOT EXISTS ix_users_created_id ON {CORE}.users (created_at, id)",
    f"CREATE INDEX IF NOT EXISTS ix_panels_created_id ON {CORE}.panels (created_at, id)",
    f"CREATE INDEX IF NOT EXISTS ix_shop_items_created_id ON {CORE}.shop_items (created_at, id)",
    f"CREATE INDEX IF NOT EXISTS ix_shop_orders_created_id ON {CORE}.shop_orders (created_at, id)",
    f"CREATE INDEX IF NOT EXISTS ix_lotteries_created_id ON {CORE}.lotteries (created_at, id)",
    f"CREATE INDEX IF NOT EXISTS ix_lottery_tickets_created_id ON {CORE}.lottery_tickets (created_at, id)",
    f"CREATE INDEX IF NOT EXISTS ix_tasks_created_id ON {CORE}.tasks (created_at, id)",
    f"CREATE INDEX IF NOT EXISTS ix_task_submissions_created_id ON {CORE}.task_submissions (created_at, id)",
    f"CREATE INDEX IF NOT EXISTS ix_referrals_created_id ON {CORE}.referrals (created_at, id)",
    f"CREATE INDEX IF NOT EXISTS ix_rating_snapshots_created_id ON {CORE}.rating_snapshots (created_at, id)",
    f"CREATE INDEX IF NOT EXISTS ix_ads_created_id ON {CORE}.ads (created_at, id)",
    f"CREATE INDEX IF NOT EXISTS ix_efhc_transfers_created_id ON {CORE}.efhc_transfers_log (created_at, id)",
    f"CREATE INDEX IF NOT EXISTS ix_ton_inbox_created_id ON {CORE}.ton_inbox_logs (created_at, id)",
]


def upgrade() -> None:
    """Создать индексы под курсоры (id/created_at)."""

    bind = op.get_bind()
    for stmt in INDEX_SQL:
        logger.info("Ensuring index", extra={"sql": stmt})
        bind.execute(text(stmt))


def downgrade() -> None:
    """Удалить созданные индексы (если нужны откаты)."""

    bind = op.get_bind()
    for stmt in INDEX_SQL:
        name = stmt.split(" IF NOT EXISTS ")[1].split(" ON ")[0]
        bind.execute(text(f"DROP INDEX IF EXISTS {CORE}.{name}"))


# ============================================================================
# Пояснения «для чайника»:
#   • Индексы нужны для cursor-based пагинации (ORDER BY created_at,id).
#   • IF NOT EXISTS делает миграцию идемпотентной — повтор не ломает БД.
#   • Денежных операций нет; это чистый DDL.
# ============================================================================
