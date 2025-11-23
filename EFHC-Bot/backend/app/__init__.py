"""FastAPI application factory for EFHC Bot."""

from __future__ import annotations

from fastapi import FastAPI

from .core.config_core import GEN_PER_SEC_BASE_KWH, GEN_PER_SEC_VIP_KWH
from .core.logging_core import get_logger
from .core.system_locks import MonetaryIdempotencyMiddleware, assert_per_sec_canon
from .routes import ads_routes, exchange_routes, lotteries_routes, panels_routes, rating_routes, shop_routes, tasks_routes, user_routes

logger = get_logger(__name__)


def create_app() -> FastAPI:
    """Instantiate FastAPI application with canonical middleware and routers."""

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
        return {"status": "ok"}

    logger.info("FastAPI app initialised with canonical middleware")
    return app
