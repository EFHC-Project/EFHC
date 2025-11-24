# ============================================================================
# EFHC Bot — scheduler.archive_panels
# -----------------------------------------------------------------------------
# Назначение: фоновой перевод просроченных панелей в архив каждые ~10 минут,
#             без двойной обработки и с мягкой деградацией.
#
# Канон/инварианты:
#   • Панели живут 180 дней; просроченные переводятся в archive (is_active=false).
#   • Денежные операции здесь не выполняются; банк не трогаем.
#   • Пользователь не уходит в минус; банк может — но в этой задаче не участвует.
#
# ИИ-защита/самовосстановление:
#   • FOR UPDATE SKIP LOCKED внутри panels_service исключает гонки воркеров.
#   • Ошибки не валят цикл: логируются, тик продолжается в следующем проходе.
#   • Порционная обработка (limit) позволяет работать при больших объёмах.
#
# Запреты:
#   • Нет P2P, нет EFHC→kWh, нет банковских операций.
#   • Никаких TODO/заглушек — задача либо архивирует, либо логирует ошибку.
# ============================================================================
from __future__ import annotations

import asyncio
from random import randint
from typing import Awaitable, Callable

from ..core.database_core import lifespan_session
from ..core.logging_core import get_logger
from ..core.utils_core import utc_now
from ..services.panels_service import archive_expired_panels

logger = get_logger(__name__)


async def _run_once_guarded(limit: int = 1000) -> None:
    """Один тик архивирования с защитой от падений."""

    async with lifespan_session() as session:
        try:
            archived = await archive_expired_panels(session, limit=limit)
            await session.commit()
            logger.info(
                "archive panels tick",
                extra={"archived": archived, "at": utc_now().isoformat()},
            )
        except Exception as exc:  # noqa: BLE001 - фиксируем и продолжаем
            logger.exception(
                "archive panels tick failed",
                extra={"error": str(exc), "at": utc_now().isoformat()},
            )


async def run_once(limit: int = 1000) -> None:
    """Публичная точка для SchedulerService: один тик архивирования."""

    await _run_once_guarded(limit=limit)


async def _run_forever(
    sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
    limit: int = 1000,
) -> None:
    """Бесконечный цикл раз в 10 минут с джиттером."""

    base_sleep = 600
    while True:
        await _run_once_guarded(limit=limit)
        jitter = randint(-15, 15)
        await sleeper(max(0, base_sleep + jitter))


def run_forever(limit: int = 1000) -> None:
    """Запустить вечный цикл для CLI/entrypoint."""

    asyncio.run(_run_forever(limit=limit))


if __name__ == "__main__":
    run_forever()

# ============================================================================
# Пояснения «для чайника»:
#   • Раз в ~10 минут просроченные панели переводятся в архив порциями по 1000.
#   • Балансы EFHC не трогаются; только статус панелей и archived_at.
#   • SKIP LOCKED исключает двойную обработку при нескольких воркерах.
#   • Ошибки лишь логируются — следующий тик попробует снова.
# ============================================================================
