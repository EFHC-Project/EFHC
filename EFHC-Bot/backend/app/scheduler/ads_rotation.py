# ============================================================================
# EFHC Bot — scheduler.ads_rotation
# -----------------------------------------------------------------------------
# Назначение: служебный тик для витрины рекламы. В текущей версии старого
#             репозитория бизнес-логика ротации не поставляется, поэтому тик
#             выполняет только диагностику и не двигает деньги/балансы.
#
# Канон/инварианты:
#   • Денежные операции не выполняются; Idempotency-Key не требуется.
#   • P2P и EFHC→kWh запрещены, но модуль их не затрагивает.
#   • Тик каждые ~10 минут с джиттером.
#
# ИИ-защита/самовосстановление:
#   • Ошибки не валят цикл — только логируются.
#   • Деградация мягкая: если логики нет, тик фиксирует это в логах.
#
# Запреты:
#   • Нет прямых SQL/денежных действий; только логирование.
#   • Без TODO/заглушек — явный вывод о неактивности ротации.
# ============================================================================
from __future__ import annotations

import asyncio
from random import randint
from typing import Awaitable, Callable

from ..core.logging_core import get_logger
from ..core.utils_core import utc_now

logger = get_logger(__name__)


async def _run_once_guarded() -> None:
    """Один диагностический тик витрины рекламы."""

    try:
        logger.info(
            "ads rotation tick (noop)",
            extra={"note": "legacy repo has no rotation logic", "at": utc_now().isoformat()},
        )
    except Exception as exc:  # noqa: BLE001 - фиксируем и продолжаем
        logger.exception(
            "ads rotation tick failed",
            extra={"error": str(exc), "at": utc_now().isoformat()},
        )


async def run_once() -> None:
    """Публичная точка для SchedulerService/CLI: один диагностический тик."""

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
#   • Тик сейчас только логирует факт вызова; бизнес-логика ротации отсутствует
#     в старом коде и должна быть добавлена отдельно при появлении требований.
#   • Денег не трогаем, Idempotency-Key не нужен.
#   • Ошибки не останавливают цикл.
# ============================================================================
