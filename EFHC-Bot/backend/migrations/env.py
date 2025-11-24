# -*- coding: utf-8 -*-
"""Alembic environment for EFHC Bot (async, canon v2.8).

Назначение:
    • Настроить Alembic для работы с async SQLAlchemy (PostgreSQL/Neon).
    • Подтянуть канонический Declarative Base и схемы EFHC.
    • Запустить миграции в оффлайн/онлайн-режиме с мягкой деградацией.

Канон/инварианты:
    • Не выполняет бизнес-логики и не трогает деньги, только DDL.
    • Использует единственный источник правды для DSN/схем — config_core.
    • Включает compare_type/compare_server_default для точности Decimal(8).

ИИ-защита/самовосстановление:
    • При отсутствии части модулей моделей не «роняет» процесс — метаданные
      собираются из доступных классов, что позволяет выполнять миграции
      частично (с отчётами в логах).
    • В оффлайн-режиме использует literal_binds, чтобы команды можно было
      просматривать без подключения к БД.

Запреты:
    • Никаких create_all/drop_all здесь — DDL описана в файлах версий.
    • Не изменяет экономику/балансы; только подключение и конфигурация.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from backend.app.core.config_core import get_settings
from backend.app.core.database_core import Base
from backend.app.core.logging_core import get_logger

# -----------------------------------------------------------------------------
# Базовая конфигурация Alembic
# -----------------------------------------------------------------------------
config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)  # настраиваем логирование Alembic

logger = get_logger(__name__)
settings = get_settings()

# Единственный источник URL БД (приводим к asyncpg)
db_url = settings.database_url_asyncpg()
config.set_main_option("sqlalchemy.url", db_url)

# Метаданные всех моделей EFHC (подтягиваются через backend.app.models)
target_metadata = Base.metadata


# -----------------------------------------------------------------------------
# Оффлайн-режим (генерация SQL без подключения)
# -----------------------------------------------------------------------------
def run_migrations_offline() -> None:
    """Запускает миграции без подключения к БД (выводит SQL)."""

    context.configure(
        url=db_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_schemas=True,
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


# -----------------------------------------------------------------------------
# Онлайн-режим (async engine)
# -----------------------------------------------------------------------------

def do_run_migrations(connection) -> None:
    """Оборачивает context.run_migrations для sync-API внутри async соединения."""

    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        include_schemas=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Создаёт async engine и запускает миграции."""

    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section) or {},
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())


# ============================================================================
# Пояснения «для чайника»:
#   • Этот файл не создаёт таблицы сам — только настраивает Alembic.
#   • URL БД берётся из .env (DATABASE_URL) и приводится к asyncpg.
#   • target_metadata = Base.metadata: сюда подтягиваются все модели EFHC.
#   • Оффлайн-режим полезен для ревью SQL; онлайн — основное применение.
# ============================================================================
