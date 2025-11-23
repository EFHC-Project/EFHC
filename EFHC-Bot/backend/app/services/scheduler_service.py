"""Сервис-помощник планировщика с мягкими ретраями."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from ..core.logging_core import get_logger

logger = get_logger(__name__)


class SchedulerService:
    """Запуск задач с бесконечными мягкими ретраями каждые 10 минут."""

    def __init__(self, tick_seconds: int = 600):
        self.tick_seconds = tick_seconds

    async def run_forever(self, name: str, func: Callable[[], Any]) -> None:
        while True:
            try:
                await asyncio.to_thread(func)
                logger.info("scheduler task completed", extra={"task": name})
            except Exception as exc:  # noqa: BLE001
                logger.error("scheduler task failed", extra={"task": name, "error": str(exc)})
            await asyncio.sleep(self.tick_seconds)
