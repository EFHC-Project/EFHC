# -*- coding: utf-8 -*-
"""Rating snapshots safety net (canon v2.8).

Назначение:
    • Убедиться, что таблица rating_snapshots существует и пригодна для
      курсорной пагинации и расчёта рейтингов «Я + TOP-100».
    • Добавить индексы под snapshot_at/bucket/id, если они отсутствуют.

Канон/инварианты:
    • Таблица не двигает деньги; хранит агрегаты total_generated_kwh.
    • Курсорная пагинация по (created_at, id) и уникальность snapshot_at+user.

ИИ-защита:
    • checkfirst/IF NOT EXISTS предотвращают падения на повторном применении.
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

from backend.app.core.config_core import get_settings
from backend.app.core.database_core import Base
from backend.app.core.logging_core import get_logger
from backend.app.models import MODEL_REGISTRY  # гарантирует загрузку моделей

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: str | None = "0003"
branch_labels = None
depends_on = None

logger = get_logger(__name__)
settings = get_settings()
CORE = getattr(settings, "DB_SCHEMA_CORE", "efhc_core")
_ = MODEL_REGISTRY


def upgrade() -> None:
    """Создать/проверить rating_snapshots и индексы."""

    bind = op.get_bind()
    table = Base.metadata.tables.get(f"{CORE}.rating_snapshots")
    if table is None:
        # Мягкое создание, если метаданные не подгружены или таблицы нет
        op.execute(
            text(
                f"""
                CREATE TABLE IF NOT EXISTS {CORE}.rating_snapshots (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    telegram_id BIGINT,
                    total_generated_kwh NUMERIC(30,8) NOT NULL DEFAULT 0,
                    rank_position INTEGER NOT NULL,
                    bucket INTEGER NOT NULL DEFAULT 0,
                    snapshot_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    CONSTRAINT uq_rating_snapshot_user_at UNIQUE (snapshot_at, user_id),
                    CONSTRAINT uq_rating_top_user_at_bucket UNIQUE (bucket, snapshot_at, user_id),
                    CONSTRAINT uq_rating_top_pos_at_bucket UNIQUE (bucket, snapshot_at, rank_position)
                )
                """
            )
        )
    else:
        table.create(bind=bind, checkfirst=True)

    # Индексы под курсоры и выборки
    op.execute(
        text(
            f"CREATE INDEX IF NOT EXISTS ix_rating_snapshots_created_id ON {CORE}.rating_snapshots (created_at, id)"
        )
    )
    op.execute(
        text(
            f"CREATE INDEX IF NOT EXISTS ix_rating_snapshots_bucket_rank ON {CORE}.rating_snapshots (bucket, rank_position)"
        )
    )


def downgrade() -> None:
    """Удалить индексы и таблицу rating_snapshots."""

    op.execute(text(f"DROP INDEX IF EXISTS {CORE}.ix_rating_snapshots_bucket_rank"))
    op.execute(text(f"DROP INDEX IF EXISTS {CORE}.ix_rating_snapshots_created_id"))
    op.execute(text(f"DROP TABLE IF EXISTS {CORE}.rating_snapshots CASCADE"))


# ============================================================================
# Пояснения «для чайника»:
#   • Таблица хранит снепшоты total_generated_kwh для рейтинга; денег не трогает.
#   • IF NOT EXISTS делает миграцию безопасной при повторных применениях.
#   • При откате таблица удаляется; бизнес-данные рейтинга восстановятся
#     пересчётом через сервис/планировщик.
# ============================================================================
