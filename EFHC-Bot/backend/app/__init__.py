# ==============================================================================
# EFHC Bot — FastAPI application factory
# ------------------------------------------------------------------------------
# Назначение: создаёт и конфигурирует FastAPI-приложение с каноном EFHC v2.8,
# подключает обязательные middleware и роутеры публичных разделов.
#
# Канон/инварианты:
#   • Проверка пер-секундных ставок через assert_per_sec_canon перед созданием.
#   • Денежные POST/PUT требуют Idempotency-Key (MonetaryIdempotencyMiddleware
#     + проверки на маршрутах).
#   • Все GET списков отдают ETag и используют курсорную пагинацию (реализовано
#     в соответствующих routers/services).
#   • Балансы меняет только банковский сервис; этот модуль не совершает
#     финансовых операций.
#
# ИИ-защеты/самовосстановление:
#   • Инициализация повторяема и идемпотентна: create_app() можно вызывать
#     несколько раз без изменения состояния.
#   • Логгер фиксирует запуск; при ошибках инициализации FastAPI поднимет
#     исключение и остановит процесс, избегая «тихих» падений.
#
# Запреты:
#   • Нет P2P, нет EFHC→kWh, нет прямых банковских транзакций в фабрике.
#   • Не запускает планировщики и бот — только HTTP-API.
# ==============================================================================
from __future__ import annotations

from fastapi import FastAPI

from .core.config_core import GEN_PER_SEC_BASE_KWH, GEN_PER_SEC_VIP_KWH
from .core.logging_core import get_logger
from .core.system_locks import MonetaryIdempotencyMiddleware, assert_per_sec_canon
from .routes import (
    ads_routes,
    exchange_routes,
    lotteries_routes,
    panels_routes,
    rating_routes,
    shop_routes,
    tasks_routes,
    user_routes,
)

logger = get_logger(__name__)


def create_app() -> FastAPI:
    """Создать FastAPI-приложение с каноническими middleware и роутерами."""

    assert_per_sec_canon(GEN_PER_SEC_BASE_KWH, GEN_PER_SEC_VIP_KWH)
    app = FastAPI(title="EFHC Bot", version="2.8")
    app.add_middleware(MonetaryIdempotencyMiddleware)

    app.include_router(user_routes.router, prefix="/users", tags=["users"])
    app.include_router(exchange_routes.router, prefix="/exchange", tags=["exchange"])
    app.include_router(panels_routes.router, prefix="/panels", tags=["panels"])
    app.include_router(shop_routes.router, prefix="/shop", tags=["shop"])
    app.include_router(lotteries_routes.router, prefix="/lotteries", tags=["lotteries"])
    app.include_router(tasks_routes.router, prefix="/tasks", tags=["tasks"])
    app.include_router(rating_routes.router, prefix="/rating", tags=["rating"])
    app.include_router(ads_routes.router, prefix="/ads", tags=["ads"])

    @app.get("/health", tags=["health"])
    async def health() -> dict[str, str]:
        """Простая проверка живости сервиса без побочных эффектов."""

        return {"status": "ok"}

    logger.info("FastAPI app initialised with canonical middleware")
    return app


# ==============================================================================
# Пояснения «для чайника»:
#   • Этот модуль ничего не пишет в БД и не двигает деньги — только конфигурирует API.
#   • Idempotency-Key проверяется middleware + зависимость на денежных маршрутах.
#   • ETag/курсорная пагинация реализованы в routers/services и подключаются здесь.
#   • Планировщики и бот запускаются отдельными процессами.
# ==============================================================================
