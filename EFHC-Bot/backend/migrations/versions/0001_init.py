# -*- coding: utf-8 -*-
"""Initial migration for EFHC Bot (canon v2.8).

Назначение:
    • Создать все схемы и таблицы EFHC согласно текущим моделям.
    • Задать базовые ограничения/индексы из ORM-моделей (Decimal(8), уникальные
      idempotency_key/tx_hash и др.).

Канон/инварианты:
    • Денежные операции и балансы не изменяются — только DDL.
    • Таблицы создаются через Declarative Base, что исключает расхождение между
      миграцией и моделями.

ИИ-защита:
    • Использует checkfirst=True, чтобы повторный запуск не ломал БД.
    • Создаёт схемы IF NOT EXISTS по списку из настроек (core/admin/etc.).

Запреты:
    • Нет ручного create_all вне Alembic; здесь единственная точка создания.
"""

from __future__ import annotations

from typing import Iterable, Set

from alembic import op
from sqlalchemy import text

from backend.app.core.config_core import get_settings
from backend.app.core.database_core import Base
from backend.app.core.logging_core import get_logger
from backend.app.models import MODEL_REGISTRY  # гарантирует загрузку моделей

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: str | None = None
branch_labels = None
depends_on = None

logger = get_logger(__name__)
settings = get_settings()

SCHEMAS: Set[str] = {
    getattr(settings, "DB_SCHEMA_CORE", "efhc_core"),
    getattr(settings, "DB_SCHEMA_ADMIN", "efhc_admin"),
    getattr(settings, "DB_SCHEMA_REFERRAL", "efhc_referral"),
    getattr(settings, "DB_SCHEMA_LOTTERY", "efhc_lottery"),
    getattr(settings, "DB_SCHEMA_TASKS", "efhc_tasks"),
}
SCHEMAS = {s for s in SCHEMAS if s}
# «Используем» реестр, чтобы избежать предупреждений линтера и заодно
# удостовериться, что импорт моделей случился на этапе импорта модуля.
_ = MODEL_REGISTRY


def _create_schemas() -> None:
    bind = op.get_bind()
    for schema in SCHEMAS:
        logger.info("Creating schema if missing", extra={"schema": schema})
        bind.execute(text(f"CREATE SCHEMA IF NOT EXISTS {schema}"))


def _drop_schemas() -> None:
    bind = op.get_bind()
    for schema in reversed(list(SCHEMAS)):
        logger.info("Dropping schema (cascade)", extra={"schema": schema})
        bind.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))


def upgrade() -> None:
    """Создать схемы и все таблицы/индексы из моделей."""

    _create_schemas()
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind, checkfirst=True)


def downgrade() -> None:
    """Удалить таблицы EFHC и схемы (для чистого отката)."""

    bind = op.get_bind()
    Base.metadata.drop_all(bind=bind, checkfirst=True)
    _drop_schemas()


# ============================================================================
# Пояснения «для чайника»:
#   • Миграция создаёт все таблицы сразу по текущим моделям EFHC.
#   • Если часть моделей отсутствует, create_all пропустит их без ошибки, что
#     позволяет запускать миграции даже при частичной сборке.
#   • Экономика/балансы не изменяются — это чистый DDL.
# ============================================================================
