# ============================================================================
# EFHC Bot — scheduler.reports_daily
# -----------------------------------------------------------------------------
# Назначение: суточная сводка для админки (новые пользователи, притоки/оттоки
#             EFHC, TON-события), запускаемая внутри 10-минутного цикла.
#
# Канон/инварианты:
#   • Денежные операции здесь не выполняются; только чтение метрик.
#   • P2P, EFHC→kWh отсутствуют; расчёты опираются на банк/логи.
#   • DailyGate управляется SchedulerService — здесь только одиночный тик.
#
# ИИ-защита/самовосстановление:
#   • При ошибках логируем и продолжаем — цикл не падает.
#   • Агрегации деградируют мягко внутри reports_service (degraded=True).
#
# Запреты:
#   • Нет прямых SQL-изменений; только чтение.
#   • Без TODO и заглушек — минимум логируем текущий статус.
# ============================================================================
from __future__ import annotations

import asyncio
from random import randint
from typing import Awaitable, Callable

from ..core.database_core import lifespan_session
from ..core.logging_core import get_logger
from ..core.utils_core import utc_now
from ..services.reports_service import daily_admin_summary

logger = get_logger(__name__)


async def _run_once_guarded(days: int = 7) -> None:
    """Один суточный отчёт с защитой от падений."""

    async with lifespan_session() as session:
        try:
            summary = await daily_admin_summary(session, days=days)
            logger.info(
                "daily admin summary built",
                extra={"degraded": summary.degraded, "items": len(summary.items), "at": utc_now().isoformat()},
            )
        except Exception as exc:  # noqa: BLE001 - фиксируем и продолжаем
            logger.exception(
                "daily admin summary failed",
                extra={"error": str(exc), "at": utc_now().isoformat()},
            )


async def run_once(days: int = 7) -> None:
    """Публичная точка для SchedulerService: один отчётный тик."""

    await _run_once_guarded(days=days)


async def _run_forever(
    sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
    days: int = 7,
) -> None:
    """Бесконечный цикл ~10 минут с джиттером (если запускается отдельно)."""

    base_sleep = 600
    while True:
        await _run_once_guarded(days=days)
        jitter = randint(-15, 15)
        await sleeper(max(0, base_sleep + jitter))


def run_forever(days: int = 7) -> None:
    """Запустить вечный цикл (CLI/entrypoint)."""

    asyncio.run(_run_forever(days=days))


if __name__ == "__main__":
    run_forever()

# ============================================================================
# Пояснения «для чайника»:
#   • Раз в сутки (через DailyGate) строится суточная витрина метрик для админки.
#   • Денег не трогаем; читаем агрегаты из логов/банка.
#   • При ошибках degraded=True, но цикл продолжает работать.
# ============================================================================
