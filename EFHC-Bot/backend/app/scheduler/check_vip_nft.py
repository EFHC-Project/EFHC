"""Планировщик проверки VIP NFT."""

from __future__ import annotations

import asyncio

from ..core.logging_core import get_logger

logger = get_logger(__name__)


def run() -> None:
    """Запуск проверки (каркас)."""

    logger.info("VIP NFT check tick executed")


if __name__ == "__main__":
    asyncio.run(asyncio.to_thread(run))
