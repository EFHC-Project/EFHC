# ============================================================================
# EFHC Bot — scheduler.generate_energy
# -----------------------------------------------------------------------------
# Назначение: фоновый тик каждые 10 минут, начисляющий kWh всем активным панелям
# по пер-секундным ставкам и запускающий rescue/health диагностику.
#
# Канон/инварианты:
#   • Только per-sec GEN_PER_SEC_BASE_KWH / GEN_PER_SEC_VIP_KWH; никаких дневных.
#   • Денежные балансы не трогаем; начисляем лишь kWh и обновляем last_tick_at.
#   • Пользователь не уходит в минус; банк может (не задействован здесь).
#   • Идемпотентность: расчёт идёт от last_tick_at, повтор тика не удвоит начисление.
#
# ИИ-защиты/самовосстановление:
#   • Advisory-лок и SKIP LOCKED предотвращают дубль-тик несколькими воркерами.
#   • Мягкая деградация: любые исключения логируются, цикл не падает.
#   • rescue_* чинят last_tick_at и экспирации; health_snapshot даёт алёрты.
#
# Запреты:
#   • Нет P2P и EFHC→kWh; денежные операции идут только через банк (не здесь).
#   • Без TODO и «суточной генерации».
# ============================================================================
from __future__ import annotations

import asyncio
from typing import Awaitable, Callable

from ..core.database_core import lifespan_session
from ..core.logging_core import get_logger
from ..core.utils_core import utc_now
from ..services.energy_service import (
    EnergyService,
    health_snapshot,
    rescue_fill_null_last_tick,
    rescue_fix_last_generated_after_expire,
)

logger = get_logger(__name__)


async def _run_once_guarded() -> None:
    """Один защитный тик генерации и самолечения."""

    async with lifespan_session() as session:
        service = EnergyService(session)
        await service.rescue_fill_null_last_tick()
        await service.rescue_fix_last_generated_after_expire()
        await service.backfill_all(tick_seconds=600)
        snapshot = await health_snapshot(session)
        logger.info("energy snapshot", extra={"snapshot": snapshot, "at": utc_now().isoformat()})


async def run_once() -> None:
    """Публичная точка для SchedulerService: один тик с rescue/health."""

    await _run_once_guarded()


async def _run_forever(sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
    """Бесконечный цикл с мягкими ретраями и контролем тика."""

    tick_seconds = 600
    while True:
        try:
            await _run_once_guarded()
        except Exception as exc:  # noqa: BLE001 - фиксируем сбой, продолжаем цикл
            logger.exception("energy scheduler tick failed but will retry", extra={"error": str(exc)})
        await sleeper(tick_seconds)


def run_forever() -> None:
    """Точка входа для планировщика: безопасный цикл без падений."""

    asyncio.run(_run_forever())


if __name__ == "__main__":
    run_forever()

# ============================================================================
# Пояснения «для чайника»:
#   • Цикл каждые 600 секунд догоняет всю пропущенную генерацию без дублей.
#   • Балансы EFHC не трогаются, только kWh и last_tick_at.
#   • Ошибки не валят процесс: логируются и перезапускают тик.
#   • Advisory-лок/skip-locked внутри сервиса защищают от гонок нескольких воркеров.
# ============================================================================
