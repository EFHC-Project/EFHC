"""Async database session management."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from .config_core import get_core_config


class Base(DeclarativeBase):
    """Declarative base for all SQLAlchemy models."""


_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    """Create (or reuse) a singleton async engine."""

    global _engine, _session_factory
    if _engine is None:
        cfg = get_core_config()
        _engine = create_async_engine(cfg.database_url.unicode_string(), future=True, echo=False)
        _session_factory = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return the session factory initialised with the engine."""

    if _session_factory is None:
        get_engine()
    assert _session_factory is not None
    return _session_factory


@asynccontextmanager
async def lifespan_session() -> AsyncIterator[AsyncSession]:
    """Async context manager yielding a database session."""

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
