# ============================================================================
# EFHC Bot — scheduler.check_vip_nft
# -----------------------------------------------------------------------------
# Назначение: регулярная синхронизация VIP-статуса по NFT коллекции с мягкими
#             ретраями и health-снимком. Денежные операции здесь не выполняются.
#
# Канон/инварианты:
#   • VIP определяется только наличием NFT из канонической коллекции.
#   • Планировщик не меняет балансы и не двигает EFHC; лишь обновляет users.is_vip.
#   • Тик раз в 10 минут; обрабатываются все пользователи с ton_wallet без
#     фильтра «последние N минут».
#
# ИИ-защита/самовосстановление:
#   • Ошибки не валят цикл: логируются, следующий тик продолжает работу.
#   • Используются батчи внутри сервиса, чтобы не держать долгие транзакции.
#   • Health-сводка (with_wallet/vip) для мониторинга и алёртов.
#
# Запреты:
#   • Нет P2P, нет EFHC→kWh, нет автодоставки NFT — только проверка факта владения.
#   • Не создаём банковских транзакций.
# ============================================================================
from __future__ import annotations

import asyncio
from random import randint
from typing import Awaitable, Callable

from ..core.database_core import lifespan_session
from ..core.logging_core import get_logger
from ..core.utils_core import utc_now
from ..services.nft_check_service import check_all_users_once, vip_health_snapshot

logger = get_logger(__name__)


async def _run_once_guarded() -> None:
    """Один тик проверки VIP с логированием, без падений цикла."""

    async with lifespan_session() as session:
        try:
            stats = await check_all_users_once(session, force_refresh=False)
            health = await vip_health_snapshot(session)
            logger.info(
                "vip nft tick finished",
                extra={"stats": stats, "health": health, "at": utc_now().isoformat()},
            )
        except Exception as exc:  # noqa: BLE001 - фиксируем и продолжаем
            logger.exception(
                "vip nft tick failed",
                extra={"error": str(exc), "at": utc_now().isoformat()},
            )


async def run_once() -> None:
    """Публичная точка для SchedulerService: один защитный тик."""

    await _run_once_guarded()


async def _run_forever(sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
    """Бесконечный цикл с джиттером 10-минутного тика."""

    base_sleep = 600
    while True:
        await _run_once_guarded()
        jitter = randint(-15, 15)
        await sleeper(max(0, base_sleep + jitter))


def run_forever() -> None:
    """Запустить вечный цикл, совместимый с CLI/entrypoint."""

    asyncio.run(_run_forever())


if __name__ == "__main__":
    run_forever()

# ============================================================================
# Пояснения «для чайника»:
#   • Цикл каждые ~10 минут проходит всех пользователей с ton_wallet и обновляет
#     флаг is_vip, не трогая деньги.
#   • Ошибки не останавливают процесс: только логируются.
#   • Health-снимок показывает сколько кошельков привязано и сколько VIP.
# ============================================================================
