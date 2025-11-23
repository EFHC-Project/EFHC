"""Common FastAPI dependencies."""

from __future__ import annotations

from typing import AsyncIterator

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from .database_core import lifespan_session


async def get_db() -> AsyncIterator[AsyncSession]:
    """Yield a database session for request scope."""

    async with lifespan_session() as session:
        yield session
