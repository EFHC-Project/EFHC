"""Scheduler task."""

from __future__ import annotations

import asyncio

from ..core.logging_core import get_logger

logger = get_logger(__name__)


def run() -> None:
    """Выполнить один тик задачи (каркас самовосстановления)."""

    logger.info(f"scheduler tick executed for {__name__}")


if __name__ == "__main__":
    asyncio.run(asyncio.to_thread(run))
