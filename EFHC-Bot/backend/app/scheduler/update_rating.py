# ============================================================================
# EFHC Bot — scheduler.update_rating
# -----------------------------------------------------------------------------
# Назначение: периодическое перестроение materialized рейтинга (Я + TOP) и
#             запись снепшота для ускорения выдачи. Денежные операции не трогаем.
#
# Канон/инварианты:
#   • Истина рейтинга — total_generated_kwh; никаких OFFSET, только keyset.
#   • Балансы не изменяются; только материализация rating_cache и snapshot meta.
#   • Тик ~10 минут; время — триггер, а не фильтр данных.
#
# ИИ-защита/самовосстановление:
#   • Advisory-lock в ranks_service защищает от гонок нескольких воркеров.
#   • Ошибки логируются, цикл не падает; при отсутствии кэша — мягкая деградация.
#
# Запреты:
#   • Нет P2P, нет EFHC→kWh; модуль не двигает деньги.
#   • Никаких «суточных» пересчётов — только per-sec основа total_generated_kwh.
# ============================================================================
from __future__ import annotations

import asyncio
from random import randint
from typing import Awaitable, Callable

from ..core.database_core import lifespan_session
from ..core.logging_core import get_logger
from ..core.utils_core import utc_now
from ..services.ranks_service import rebuild_rank_snapshot

logger = get_logger(__name__)


async def _run_once_guarded() -> None:
    """Один тик перестроения рейтинга с защитой от падений."""

    async with lifespan_session() as session:
        try:
            meta = await rebuild_rank_snapshot(session)
            await session.commit()
            logger.info(
                "rating snapshot rebuilt",
                extra={"meta": meta, "at": utc_now().isoformat()},
            )
        except Exception as exc:  # noqa: BLE001 - фиксируем и продолжаем
            logger.exception(
                "rating snapshot rebuild failed",
                extra={"error": str(exc), "at": utc_now().isoformat()},
            )


async def run_once() -> None:
    """Публичная точка для SchedulerService: один тик обновления рейтинга."""

    await _run_once_guarded()


async def _run_forever(sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
    """Бесконечный цикл с джиттером 10-минутного расписания."""

    base_sleep = 600
    while True:
        await _run_once_guarded()
        jitter = randint(-15, 15)
        await sleeper(max(0, base_sleep + jitter))


def run_forever() -> None:
    """Запустить вечный цикл для CLI/entrypoint."""

    asyncio.run(_run_forever())


if __name__ == "__main__":
    run_forever()

# ============================================================================
# Пояснения «для чайника»:
#   • Каждые ~10 минут рейтинг пересчитывается и кладётся в rating_cache.
#   • Денег не затрагивает; только чтение users.total_generated_kwh.
#   • Advisory-lock внутри сервиса защищает от дублей; ошибки только в логах.
# ============================================================================
