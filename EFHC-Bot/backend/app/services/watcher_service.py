"""TON watcher service with идемпотентностью."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from ..core.logging_core import get_logger

logger = get_logger(__name__)


class WatcherService:
    """Обработчик входящих TON-платежей (каркас)."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def tick(self) -> None:
        """Сделать один проход по неподтверждённым логам.

        Полная интеграция с TON API будет добавлена позже, здесь фиксируем
        каркас и логи для самовосстановления.
        """
+
+        logger.info("watcher tick executed")
