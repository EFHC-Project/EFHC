# ============================================================================
# EFHC Bot — scheduler.tasks_autorestart
# -----------------------------------------------------------------------------
# Назначение: служебный тик для заданий/модерации. В старом коде отдельной
#             логики автоперезапуска не было, поэтому задача сейчас выполняет
#             только диагностику без изменения балансов.
#
# Канон/инварианты:
#   • Денежные операции не выполняются; все выплаты за задания делаются через
#     tasks_service/transactions_service, но здесь они не вызываются.
#   • P2P и EFHC→kWh запрещены, но модуль их не затрагивает.
#   • Тик ~10 минут с джиттером.
#
# ИИ-защита/самовосстановление:
#   • Ошибки не валят цикл — только логируются.
#   • При добавлении реальной логики нужно обрабатывать все записи со статусом
#     не финальным и next_retry_at <= now (догон, а не «последние N минут»).
#
# Запреты:
#   • Нет прямых SQL/денежных действий; только логирование текущего состояния.
#   • Без TODO — явно указываем диагностический режим.
# ============================================================================
from __future__ import annotations

import asyncio
from random import randint
from typing import Awaitable, Callable

from ..core.logging_core import get_logger
from ..core.utils_core import utc_now

logger = get_logger(__name__)


async def _run_once_guarded() -> None:
    """Диагностический тик заданий (без бизнес-логики)."""

    try:
        logger.info(
            "tasks autorestart tick (noop)",
            extra={"note": "legacy repo has no auto-restart logic", "at": utc_now().isoformat()},
        )
    except Exception as exc:  # noqa: BLE001 - фиксируем и продолжаем
        logger.exception(
            "tasks autorestart tick failed",
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
#   • Сейчас тик только логирует вызов; реальные выплаты/модерация делаются через
#     tasks_service и требуют Idempotency-Key — но здесь они не вызываются.
#   • Денег не трогаем, ошибок не боимся — цикл не падает.
# ============================================================================
