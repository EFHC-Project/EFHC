# -*- coding: utf-8 -*-
# backend/app/core/database_core.py
# =============================================================================
# Назначение кода:
#   • Единая точка работы с БД EFHC Bot (PostgreSQL + asyncpg + SQLAlchemy 2.0).
#   • Создание и конфигурация AsyncEngine и async_sessionmaker.
#   • Безопасная выдача сессий для FastAPI-роутов и сервисов.
#   • Базовые health- и self-healing-утилиты (ping, мягкий реинициализатор).
#
# Канон / инварианты EFHC:
#   • Только async-движок (create_async_engine), никаких sync-engine.
#   • DSN берём из Settings.database_url_asyncpg() — там единый источник истины.
#   • Пул соединений управляется настройками DB_POOL_SIZE / DB_MAX_OVERFLOW.
#   • Сессии expire_on_commit=False (во избежание лишних рефрешей).
#
# ИИ-защита:
#   • При проблемах с созданием движка/сессии — подробный лог и понятные
#     исключения, без скрытого «молчаливого» падения.
#   • Функция db_ping() для healthcheck и самопроверки перед стартом воркеров.
#   • Лёгкий self-healing: reset_engine() позволяет аккуратно пересоздать
#     движок при смене настроек или после серьёзных сбоев.
#
# Запреты:
#   • Никакой бизнес-логики (эмиссия, списания, банк и т.п.) в этом модуле.
#   • Никаких Alembic-миграций/DDL здесь — только подключения и сессии.
# =============================================================================

from __future__ import annotations

import asyncio
from typing import AsyncGenerator, Optional

from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from backend.app.core.config_core import get_settings
from backend.app.core.logging_core import get_logger

logger = get_logger(__name__)
settings = get_settings()

# -----------------------------------------------------------------------------
# Глобальные объекты: движок и фабрика сессий
# -----------------------------------------------------------------------------
_engine: Optional[AsyncEngine] = None
_SessionFactory: Optional[async_sessionmaker[AsyncSession]] = None
_engine_lock = asyncio.Lock()


def _create_engine() -> AsyncEngine:
    """
    Создаёт новый AsyncEngine на базе актуальных настроек.

    Особенности:
    • DSN приводится к asyncpg-формату через Settings.database_url_asyncpg().
    • Включён pool_pre_ping для раннего обнаружения "умерших" соединений.
    • echo включается только в DEBUG-режиме.
    """
    dsn = settings.database_url_asyncpg()
    logger.info("Creating async DB engine", extra={"dsn_set": bool(dsn)})
    engine = create_async_engine(
        dsn,
        pool_size=settings.DB_POOL_SIZE,
        max_overflow=settings.DB_MAX_OVERFLOW,
        pool_pre_ping=True,
        echo=settings.DEBUG,
    )
    return engine


def _create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """
    Строит async_sessionmaker поверх переданного движка.

    Канон:
    • expire_on_commit=False — объекты остаются валидными после commit().
    • autoflush=False — явный контроль flush при необходимости.
    """
    return async_sessionmaker(
        bind=engine,
        expire_on_commit=False,
        autoflush=False,
        autocommit=False,
        class_=AsyncSession,
    )


async def reset_engine() -> None:
    """
    Мягко пересоздаёт движок и фабрику сессий.

    Использование:
    • при критических сбоях подключения;
    • при горячем обновлении конфигурации DSN (редкий случай).

    ИИ-защита:
    • Закрывает старый engine через dispose(), чтобы не оставлять
      "висящие" соединения.
    """
    global _engine, _SessionFactory

    async with _engine_lock:
        old_engine = _engine
        try:
            new_engine = _create_engine()
            _SessionFactory = _create_session_factory(new_engine)
            _engine = new_engine
            logger.info("DB engine has been reset successfully")
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to reset DB engine", extra={"error": str(exc)})
            # Если новый движок не поднялся — откатываемся к старому
            if old_engine is not None:
                _engine = old_engine
            raise
        else:
            if old_engine is not None:
                try:
                    await old_engine.dispose()
                except Exception:  # noqa: BLE001
                    logger.warning("Error during old engine dispose", exc_info=True)


def get_engine() -> AsyncEngine:
    """
    Возвращает текущий AsyncEngine.

    Если движок ещё не был создан (например, при раннем импортировании),
    создаёт его синхронно. Для продакшна рекомендуется вызывать db_ping()
    на старте, чтобы гарантировать живое подключение.
    """
    global _engine, _SessionFactory

    if _engine is None:
        engine = _create_engine()
        _engine = engine
        _SessionFactory = _create_session_factory(engine)
        logger.info("DB engine lazily initialized")
    assert _engine is not None  # для mypy
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """
    Возвращает фабрику сессий.

    Гарантирует, что движок создан (через get_engine()).
    """
    global _SessionFactory

    if _SessionFactory is None:
        engine = get_engine()
        _SessionFactory = _create_session_factory(engine)
        logger.info("Session factory initialized")
    assert _SessionFactory is not None  # для mypy
    return _SessionFactory


# -----------------------------------------------------------------------------
# FastAPI-совместимая зависимость: выдача сессии
# -----------------------------------------------------------------------------
async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    Зависимость для FastAPI-роутов и сервисов.

    Пример использования:
        from typing import Annotated
        from fastapi import Depends
        from backend.app.core.database_core import get_db
        from sqlalchemy.ext.asyncio import AsyncSession

        SessionDep = Annotated[AsyncSession, Depends(get_db)]

        @router.get("/users")
        async def list_users(db: SessionDep):
            ...

    ИИ-защита:
    • При ошибке логируем контекст и пробрасываем исключение наверх;
      это позволит внешнему коду принять решение (ретрай/ошибка API).
    """
    session_factory = get_session_factory()
    session = session_factory()
    try:
        yield session
        # commit управляется вызывающим кодом; здесь не коммитим
    except Exception as exc:  # noqa: BLE001
        logger.exception("DB session error", extra={"error": str(exc)})
        raise
    finally:
        try:
            await session.close()
        except Exception:  # noqa: BLE001
            logger.warning("Failed to close DB session", exc_info=True)


# -----------------------------------------------------------------------------
# Health-check / ping
# -----------------------------------------------------------------------------
async def db_ping() -> bool:
    """
    Простейший health-check БД.

    Возвращает:
    • True — если SELECT 1 успешно прошёл;
    • False — если движок не создан или БД не отвечает.

    Использование:
    • endpoint /health;
    • проверка перед запуском фоновых воркеров/планировщика.
    """
    engine = get_engine()
    try:
        async with engine.connect() as conn:
            await conn.execute("SELECT 1")  # type: ignore[arg-type]
        return True
    except (OperationalError, DBAPIError) as exc:
        logger.error(
            "DB ping failed: DB is not reachable",
            extra={"error": str(exc)},
        )
        return False
    except Exception as exc:  # noqa: BLE001
        logger.exception("DB ping failed with unexpected error", extra={"error": str(exc)})
        return False


# -----------------------------------------------------------------------------
# Инициализация при импорте (lazy-friendly)
# -----------------------------------------------------------------------------
# ВАЖНО:
# • Мы не создаём движок жёстко при импорте, чтобы не ломать миграции/Alembic
#   и не заставлять поднимать БД в любых вспомогательных скриптах.
# • get_engine()/get_session_factory() создадут движок лениво при первом вызове.
# =============================================================================

__all__ = [
    "AsyncSession",
    "AsyncEngine",
    "get_engine",
    "get_session_factory",
    "get_db",
    "db_ping",
    "reset_engine",
]
