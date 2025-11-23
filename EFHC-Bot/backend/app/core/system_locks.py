"""System-level invariants and middleware helpers."""

from __future__ import annotations

from decimal import Decimal
from typing import Awaitable, Callable

from fastapi import Depends, Header, Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from .config_core import GEN_PER_SEC_BASE_KWH, GEN_PER_SEC_VIP_KWH
from .errors_core import IdempotencyError


def assert_per_sec_canon(base: Decimal, vip: Decimal) -> None:
    """Validate generation constants to match canonical EFHC values."""

    if base != GEN_PER_SEC_BASE_KWH or vip != GEN_PER_SEC_VIP_KWH:
        raise ValueError("Per-second generation constants must match EFHC canon v2.8")


async def require_idempotency_header(
    idempotency_key: str | None = Header(default=None, convert_underscores=False)
) -> str:
    """Dependency enforcing Idempotency-Key presence."""

    if not idempotency_key:
        raise IdempotencyError()
    return idempotency_key


class MonetaryIdempotencyMiddleware(BaseHTTPMiddleware):
    """Middleware preventing monetary POST/PUT without Idempotency-Key."""

    def __init__(self, app):  # type: ignore[override]
        super().__init__(app)

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.method in {"POST", "PUT", "PATCH", "DELETE"} and request.url.path.startswith(
            ("/exchange", "/panels", "/lotteries", "/shop", "/admin", "/tasks")
        ):
            if "idempotency-key" not in request.headers:
                raise IdempotencyError()
        return await call_next(request)
