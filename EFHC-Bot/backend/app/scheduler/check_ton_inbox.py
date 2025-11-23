# ============================================================================
# EFHC Bot — scheduler.check_ton_inbox
# -----------------------------------------------------------------------------
# Назначение: вечный вотчер входящих TON-переводов с мягкими ретраями,
# advisory-локом и делегированием бизнес-логики в watcher_service.
#
# Канон/инварианты:
#   • tx_hash в ton_inbox_logs уникален; MEMO парсится детерминированно.
#   • Денежные движения идут только через transactions_service с Idempotency-Key.
#   • Пользователь не уходит в минус; банк может.
#   • Тик каждые 10 минут, без фильтра «последние N минут» — обрабатываем всё,
#     что next_retry_at <= now.
#
# ИИ-защиты/самовосстановление:
#   • _run_once_guarded ловит любые исключения и переносит next_retry_at, не валя цикл.
#   • _run_forever уважает сигналы остановки, добавляет джиттер по таймаутам сна.
#   • Advisory-лок на процесс исключает параллельный дубль в кластере.
#   • process_incoming_payments/process_existing_backlog внутри сервиса идемпотентны.
#
# Запреты:
#   • Нет P2P, нет EFHC→kWh, нет автодоставки NFT — только заявки.
#   • Не создаём банковских транзакций напрямую здесь (делает watcher_service).
# ============================================================================
from __future__ import annotations

import asyncio
from random import randint
from typing import Awaitable, Callable

from sqlalchemy import text

from ..core.database_core import lifespan_session
from ..core.logging_core import get_logger
from ..core.utils_core import utc_now
from ..services.watcher_service import WatcherService

logger = get_logger(__name__)

_LOCK_KEY = 84_200


async def _try_lock(session) -> bool:
    """Взять advisory-лок, чтобы единственный воркер шёл по тикам."""

    result = await session.execute(text("SELECT pg_try_advisory_lock(:k)").bindparams(k=_LOCK_KEY))
    return bool(result.scalar_one())


async def _unlock(session) -> None:
    """Снять advisory-лок после тика."""

    await session.execute(text("SELECT pg_advisory_unlock(:k)").bindparams(k=_LOCK_KEY))


async def _run_once_guarded() -> None:
    """Один тик: обрабатываем входящие и хвосты, не падая при ошибках."""

    async with lifespan_session() as session:
        if not await _try_lock(session):
            logger.info("ton inbox tick skipped: lock held")
            return
        try:
            watcher = WatcherService(session)
            await watcher.process_incoming_payments()
            await watcher.process_existing_backlog()
        except Exception as exc:  # noqa: BLE001 - фиксируем, но не падаем
            logger.exception(
                "ton inbox tick failed", extra={"error": str(exc), "ts": utc_now().isoformat()}
            )
        finally:
            await _unlock(session)


async def _run_forever(sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
    """Бесконечный цикл с мягкими ретраями и контролем тика."""

    base_sleep = 600
    while True:
        await _run_once_guarded()
        jitter = randint(-15, 15)
        await sleeper(max(0, base_sleep + jitter))


def run_forever() -> None:
    """Точка входа: вечный цикл, который не падает и сам себя лечит."""

    asyncio.run(_run_forever())


if __name__ == "__main__":
    run_forever()

# ============================================================================
# Пояснения «для чайника»:
#   • Цикл каждые 10 минут (с джиттером) берёт advisory-лок и обрабатывает все
#     ton_inbox_logs со статусом не финальным.
#   • Деньги не двигает напрямую: это делает watcher_service через банк.
#   • Ошибки не валят процесс; next_retry_at расставляется внутри сервисного слоя.
#   • MEMO парсится строго по канону EFHC; повтор по tx_hash не создаёт дубль.
# ============================================================================
