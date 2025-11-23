"""Глобальные замки канона EFHC: инварианты и idempotency."""

from __future__ import annotations

# ======================================================================
# EFHC Bot — core/system_locks.py
# ----------------------------------------------------------------------
# Назначение: централизованные проверки канона (per-sec ставки, запрет
#             денежных POST без Idempotency-Key) и middleware для API.
# Канон/инварианты:
#   • Пер-сек ставки должны совпадать с config_core (0.00000692/0.00000741).
#   • Любой денежный POST/PUT/PATCH/DELETE требует Idempotency-Key.
#   • Балансы не меняются здесь; модуль только валидирует и блокирует.
# ИИ-защиты/самовосстановление:
#   • Преждевременные ошибки поднимаются рано, чтобы не допускать
#     расхождений в экономике; middleware мягко блокирует без падения сервера.
# Запреты:
#   • Нет P2P, нет EFHC→kWh; модуль не выполняет списаний/зачислений.
# ======================================================================

from decimal import Decimal
from typing import Awaitable, Callable

from fastapi import Depends, Header, Request
from starlette.middleware.base import (
    BaseHTTPMiddleware,
    RequestResponseEndpoint,
)
from starlette.responses import Response

from .config_core import GEN_PER_SEC_BASE_KWH, GEN_PER_SEC_VIP_KWH
from .errors_core import IdempotencyError


def assert_per_sec_canon(base: Decimal, vip: Decimal) -> None:
    """Проверить, что ставки генерации совпадают с каноном EFHC v2.8.

    Назначение: защитить от случайной подмены суточных или иных ставок.
    Вход: значения базовой и VIP ставок per-second.
    Побочные эффекты: отсутствуют; при несоответствии выбрасывает ValueError.
    Идемпотентность: детерминированно, повторяемо.
    Исключения: ValueError при несовпадении констант.
    """

    if base != GEN_PER_SEC_BASE_KWH or vip != GEN_PER_SEC_VIP_KWH:
        raise ValueError(
            "Per-second generation constants must match EFHC canon v2.8"
        )


async def require_idempotency_header(
    idempotency_key: str | None = Header(
        default=None, convert_underscores=False
    ),
) -> str:
    """FastAPI Depends, требующий заголовок Idempotency-Key.

    Назначение: защитить все денежные POST/PUT/PATCH/DELETE от дублей.
    Вход: заголовок ``Idempotency-Key`` (без преобразования подчёркиваний).
    Выход: строковое значение ключа.
    Побочные эффекты: нет, кроме валидации.
    Исключения: IdempotencyError при отсутствии заголовка.
    """

    if not idempotency_key:
        raise IdempotencyError()
    return idempotency_key


class MonetaryIdempotencyMiddleware(BaseHTTPMiddleware):
    """Middleware, блокирующее денежные запросы без Idempotency-Key."""

    def __init__(self, app):  # type: ignore[override]
        super().__init__(app)

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        """Проверить обязательность Idempotency-Key для денежных путей.

        Назначение: страховой слой поверх Depends, чтобы даже пропущенный
        dependency не позволил провести транзакцию без ключа.
        Вход: HTTP-запрос; проверяются методы POST/PUT/PATCH/DELETE и пути
        денежных доменов (exchange/panels/lotteries/shop/admin/tasks).
        Побочные эффекты: при отсутствии заголовка поднимается IdempotencyError
        до попадания в роут/сервис.
        Идемпотентность: нет изменений состояния, только валидация.
        """

        if request.method in {"POST", "PUT", "PATCH", "DELETE"} and (
            request.url.path.startswith(
                (
                    "/exchange",
                    "/panels",
                    "/lotteries",
                    "/shop",
                    "/admin",
                    "/tasks",
                )
            )
        ):
            if "idempotency-key" not in request.headers:
                raise IdempotencyError()
        return await call_next(request)


# ======================================================================
# Пояснения «для чайника»:
#   • Этот модуль не двигает деньги — только валидирует ставки и ключи.
#   • Любой денежный запрос обязан иметь Idempotency-Key, иначе 400.
#   • Пер-сек ставки проверяются на старте и при импорте сервисов.
#   • Путь EFHC→kWh и P2P здесь не допускается — это заблокировано каноном.
# ======================================================================
