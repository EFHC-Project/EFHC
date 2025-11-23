"""Async SQLAlchemy setup and session management for EFHC backend."""

from __future__ import annotations

# ======================================================================
# EFHC Bot — core/database_core.py
# ----------------------------------------------------------------------
# Назначение: централизованно инициализирует async SQLAlchemy engine,
#             Declarative Base и фабрику сессий для всех сервисов EFHC.
# Канон/инварианты:
#   • Модуль не изменяет балансы/деньги; только выдаёт подключения.
#   • Поддерживается один общий AsyncEngine, shared между воркерами.
#   • Транзакции должны оформляться на уровне сервисов/роутов.
# ИИ-защеты/самовосстановление:
#   • Lazy-инициализация и повторное использование engine исключают гонки
#     при старте и позволяют пережить временное отсутствие БД (после
#     восстановления повторный вызов переподнимет коннектор).
# Запреты:
#   • Нет P2P, нет EFHC→kWh, нет бизнес-логики или миграций здесь.
# ======================================================================

from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from .config_core import get_core_config  # единый источник DATABASE_URL


class Base(DeclarativeBase):
    """Declarative base для всех ORM-моделей EFHC."""


_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    """Создать (или вернуть существующий) singleton AsyncEngine.

    Назначение: предоставить shared-engine для всего приложения, избегая
    повторной конфигурации и гонок при старте.
    Вход: нет; читает DATABASE_URL из CoreConfig.
    Выход: готовый AsyncEngine.
    Побочные эффекты: создаёт engine и session factory при первом вызове;
    не изменяет бизнес-данные.
    Идемпотентность: повторный вызов возвращает тот же объект.
    Исключения: ошибки подключения БД пробрасываются; после восстановления
    повторный вызов корректно пересоздаст engine.
    """

    global _engine, _session_factory
    if _engine is None:
        cfg = get_core_config()
        _engine = create_async_engine(
            cfg.database_url.unicode_string(), future=True, echo=False
        )
        _session_factory = async_sessionmaker(
            _engine, expire_on_commit=False
        )
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Вернуть фабрику AsyncSession, инициализированную для EFHC.

    Назначение: единая точка получения sessionmaker; обеспечивает
    совместимость со всеми Depends и фоновыми задачами.
    Побочные эффекты: ленивое создание engine при первом вызове.
    """

    if _session_factory is None:
        get_engine()
    assert _session_factory is not None
    return _session_factory


@asynccontextmanager
async def lifespan_session() -> AsyncIterator[AsyncSession]:
    """Выдать сессию для запроса/таска с автокоммитом/rollback.

    Назначение: безопасный контекст для FastAPI Depends и фоновых сервисов.
    Побочные эффекты: открывает транзакцию; при успехе коммитит, при
    исключении откатывает, затем закрывает соединение.
    Идемпотентность: повторное использование корректно управляет своим
    контекстом и не делит транзакции между запросами.
    Исключения: любые ошибки БД/логики пробрасываются после rollback.
    """

    session_factory = get_session_factory()
    session = session_factory()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


# ======================================================================
# Пояснения «для чайника»:
#   • Этот модуль не двигает деньги и не меняет балансы.
#   • Engine создаётся лениво и переиспользуется — так меньше подключений.
#   • lifespan_session сам коммитит или делает rollback при ошибке.
#   • BUSINESS-логика (EFHC/кредиты/дебеты) живёт в services/*.
# ======================================================================
