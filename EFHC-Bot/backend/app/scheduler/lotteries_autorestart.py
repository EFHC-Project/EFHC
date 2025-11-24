# ============================================================================
# EFHC Bot — scheduler.lotteries_autorestart
# -----------------------------------------------------------------------------
# Назначение: вспомогательный тик для лотерей. В старом репозитории не было
#             отдельной логики автоперезапуска, поэтому модуль выполняет
#             диагностику без изменения балансов.
#
# Канон/инварианты:
#   • Денежные операции не выполняются; Idempotency-Key не требуется.
#   • Лотереи продают билеты только за EFHC, но здесь деньги не двигаются.
#   • Тик ~10 минут с джиттером.
#
# ИИ-защита/самовосстановление:
#   • Ошибки не валят цикл — только логируются.
#   • Если логика будет добавлена, обрабатывать нужно все записи с
#     next_retry_at <= now (догон без фильтра последних минут).
#
# Запреты:
#   • Нет P2P, нет EFHC→kWh; модуль не должен менять балансы.
#   • Без TODO — явно отмечаем диагностический режим.
# ============================================================================
from __future__ import annotations

import asyncio
from random import randint
from typing import Awaitable, Callable

from ..core.logging_core import get_logger
from ..core.utils_core import utc_now

logger = get_logger(__name__)


async def _run_once_guarded() -> None:
    """Диагностический тик для лотерей (без бизнес-логики)."""

    try:
        logger.info(
            "lotteries autorestart tick (noop)",
            extra={"note": "legacy repo has no auto-restart logic", "at": utc_now().isoformat()},
        )
    except Exception as exc:  # noqa: BLE001 - фиксируем и продолжаем
        logger.exception(
            "lotteries autorestart tick failed",
            extra={"error": str(exc), "at": utc_now().isoformat()},
        )


async def run_once() -> None:
    """Публичная точка для SchedulerService/CLI."""

    await _run_once_guarded()


async def _run_forever(sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
    """Бесконечный цикл ~10 минут с джиттером."""

    base_sleep = 600
    while True:
        await _run_once_guarded()
        jitter = randint(-15, 15)
        await sleeper(max(0, base_sleep + jitter))


def run_forever() -> None:
    """Запустить вечный цикл (CLI/entrypoint)."""

    asyncio.run(_run_forever())


if __name__ == "__main__":
    run_forever()

# ============================================================================
# Пояснения «для чайника»:
#   • Сейчас тик только логирует вызов; если понадобится автоперезапуск лотерей,
#     нужно будет использовать банковский сервис и Idempotency-Key для денежных
#     операций билетов.
#   • Денег не трогаем, ошибок не боимся — цикл не падает.
# ============================================================================
