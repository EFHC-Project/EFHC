# -*- coding: utf-8 -*-
# backend/app/core/errors_core.py
# =============================================================================
# Назначение кода:
#   • Единый слой ошибок/исключений EFHC Bot.
#   • Канонические коды ошибок для фронтенда/логов.
#   • Унифицированные JSON-ответы для FastAPI.
#
# Канон / инварианты EFHC:
#   • Денежные/игровые сервисы бросают ТОЛЬКО доменные исключения из этого
#     модуля (или LockViolation из system_locks).
#   • Клиенту никогда не утекают технические детали (stack trace, DSN, ключи).
#   • Для всех известных исключений есть стабильные error_code и http_status.
#
# ИИ-защита:
#   • Любая неизвестная ошибка логируется как INTERNAL, но наружу выдаётся
#     безопасное сообщение "internal_error" без деталей.
#   • LockViolation (нарушение канона) никогда не маскируется как 500, а
#     возвращается как 400 Bad Request с кодом "lock_violation".
#   • HTTPException пропускается, но дополняется стандартным JSON-форматом.
#
# Запреты:
#   • Не включать сюда бизнес-логику (банковские расчёты, генерацию и т.п.).
#   • Не логировать здесь секреты/конфиденциальные данные (см. logging_core).
# =============================================================================

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple, Type, Union, cast

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse

from backend.app.core.logging_core import get_logger
from backend.app.core.system_locks import LockViolation

logger = get_logger(__name__)


# -----------------------------------------------------------------------------
# Базовая доменная ошибка EFHC
# -----------------------------------------------------------------------------
@dataclass
class EFHCError(Exception):
    """
    Базовое доменное исключение EFHC.

    Поля:
      • code         — стабильный машинный код ошибки (snake_case).
      • message      — короткое безопасное сообщение для клиента.
      • http_status  — HTTP код по умолчанию (можно переопределить).
      • details      — безопасные детали (без секретов), опционально.
    """

    code: str
    message: str
    http_status: int = status.HTTP_400_BAD_REQUEST
    details: Dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> Dict[str, Any]:
        """Готовит JSON-ответ для клиента."""
        payload: Dict[str, Any] = {
            "error": self.code,
            "message": self.message,
        }
        if self.details:
            payload["details"] = self.details
        return payload

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


# -----------------------------------------------------------------------------
# Часто используемые доменные ошибки
# -----------------------------------------------------------------------------
class NotFoundError(EFHCError):
    """Ресурс не найден (панель, пользователь, лотерея и т.п.)."""

    def __init__(
        self,
        message: str = "Resource not found.",
        *,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(
            code="not_found",
            message=message,
            http_status=status.HTTP_404_NOT_FOUND,
            details=details or {},
        )


class ValidationError(EFHCError):
    """Некорректные входные данные/состояние."""

    def __init__(
        self,
        message: str = "Invalid data.",
        *,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(
            code="validation_error",
            message=message,
            http_status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            details=details or {},
        )


class BankBalanceError(EFHCError):
    """Ошибка работы с балансом банка/пользователя."""

    def __init__(
        self,
        message: str = "Balance operation error.",
        *,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(
            code="balance_error",
            message=message,
            http_status=status.HTTP_400_BAD_REQUEST,
            details=details or {},
        )


class IdempotencyConflictError(EFHCError):
    """Конфликт идемпотентности (повторная попытка с другим payload)."""

    def __init__(
        self,
        message: str = "Idempotency conflict.",
        *,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(
            code="idempotency_conflict",
            message=message,
            http_status=status.HTTP_409_CONFLICT,
            details=details or {},
        )


class LotteryClosedError(EFHCError):
    """Попытка купить билет/забрать приз в закрытой/завершённой лотерее."""

    def __init__(
        self,
        message: str = "Lottery is closed.",
        *,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(
            code="lottery_closed",
            message=message,
            http_status=status.HTTP_400_BAD_REQUEST,
            details=details or {},
        )


class PanelsLimitExceededError(EFHCError):
    """Превышен лимит панелей на пользователя."""

    def __init__(
        self,
        message: str = "Panels limit exceeded.",
        *,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(
            code="panels_limit_exceeded",
            message=message,
            http_status=status.HTTP_400_BAD_REQUEST,
            details=details or {},
        )


class ReferralError(EFHCError):
    """Ошибки реферальной системы (недопустимая ссылка, цикл и т.п.)."""

    def __init__(
        self,
        message: str = "Referral operation error.",
        *,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(
            code="referral_error",
            message=message,
            http_status=status.HTTP_400_BAD_REQUEST,
            details=details or {},
        )


class WithdrawalError(EFHCError):
    """Ошибка обработки заявки на вывод EFHC."""

    def __init__(
        self,
        message: str = "Withdrawal operation error.",
        *,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(
            code="withdrawal_error",
            message=message,
            http_status=status.HTTP_400_BAD_REQUEST,
            details=details or {},
        )


class TasksError(EFHCError):
    """Ошибка при работе с заданиями/бонусами."""

    def __init__(
        self,
        message: str = "Task operation error.",
        *,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(
            code="task_error",
            message=message,
            http_status=status.HTTP_400_BAD_REQUEST,
            details=details or {},
        )


# -----------------------------------------------------------------------------
# Нормализация исключений → (status_code, payload)
# -----------------------------------------------------------------------------
def normalize_exception(
    exc: BaseException,
) -> Tuple[int, Dict[str, Any]]:
    """
    Приводит произвольное исключение к каноническому HTTP-ответу.

    Правила:
      • EFHCError        → свой http_status + to_payload().
      • LockViolation    → 400 + {"error": "lock_violation", ...}.
      • HTTPException    → status_code + {"error": "http_error", "message", ...}.
      • Любая другая     → 500 + {"error": "internal_error"} (без деталей).
    """

    # Доменные ошибки EFHC
    if isinstance(exc, EFHCError):
        payload = exc.to_payload()
        return exc.http_status, payload

    # Нарушение канона/архитектурных запретов
    if isinstance(exc, LockViolation):
        logger.warning("LockViolation occurred: %s", str(exc))
        return (
            status.HTTP_400_BAD_REQUEST,
            {
                "error": "lock_violation",
                "message": str(exc),
            },
        )

    # Стандартные FastAPI/Starlette HTTP-ошибки
    if isinstance(exc, HTTPException):
        # detail может быть строкой или dict
        msg: str
        if isinstance(exc.detail, str):
            msg = exc.detail
            details: Dict[str, Any] = {}
        elif isinstance(exc.detail, dict):
            details = cast(Dict[str, Any], exc.detail)
            msg = details.get("message") or details.get("detail") or "HTTP error."
        else:
            msg = "HTTP error."
            details = {}

        payload = {
            "error": "http_error",
            "message": msg,
        }
        if details:
            payload["details"] = details
        return exc.status_code, payload

    # Неизвестная внутренняя ошибка: логируем максимально подробно, но
    # наружу выдаём только общий код internal_error.
    logger.exception("Unhandled exception", extra={"error_type": type(exc).__name__})
    return (
        status.HTTP_500_INTERNAL_SERVER_ERROR,
        {
            "error": "internal_error",
            "message": "Internal server error.",
        },
    )


# -----------------------------------------------------------------------------
# FastAPI-хендлеры исключений
# -----------------------------------------------------------------------------
async def efhc_error_handler(
    request: Request, exc: EFHCError
) -> JSONResponse:
    """
    Обработчик EFHCError для FastAPI.

    Возвращает структурированный JSON с кодом ошибки.
    """
    status_code, payload = normalize_exception(exc)
    logger.warning(
        "EFHCError handled",
        extra={
            "path": request.url.path,
            "error": exc.code,
            "status": status_code,
        },
    )
    return JSONResponse(status_code=status_code, content=payload)


async def lock_violation_handler(
    request: Request, exc: LockViolation
) -> JSONResponse:
    """
    Обработчик LockViolation (нарушение канона).

    Специально выделяем для явной сигнализации в логах.
    """
    status_code, payload = normalize_exception(exc)
    logger.warning(
        "LockViolation handled",
        extra={
            "path": request.url.path,
            "status": status_code,
        },
    )
    return JSONResponse(status_code=status_code, content=payload)


async def generic_exception_handler(
    request: Request, exc: Exception
) -> JSONResponse:
    """
    Обработчик "на всё остальное".

    ИИ-защита:
      • Логируем stack trace и тип исключения.
      • Клиенту отдаём только безопасный internal_error.
    """
    status_code, payload = normalize_exception(exc)
    logger.error(
        "Unhandled exception handled by generic handler",
        extra={
            "path": request.url.path,
            "status": status_code,
            "exc_type": type(exc).__name__,
        },
    )
    return JSONResponse(status_code=status_code, content=payload)


# -----------------------------------------------------------------------------
# Регистрация хендлеров в приложении FastAPI
# -----------------------------------------------------------------------------
def setup_exception_handlers(app: FastAPI) -> None:
    """
    Подключает все необходимые обработчики исключений.

    Вызывать один раз при создании приложения:
        app = FastAPI(...)
        setup_exception_handlers(app)
    """
    app.add_exception_handler(EFHCError, efhc_error_handler)
    app.add_exception_handler(LockViolation, lock_violation_handler)
    app.add_exception_handler(Exception, generic_exception_handler)

    logger.info("Exception handlers registered for EFHCError/LockViolation/Exception")


# =============================================================================
# Пояснения «для чайника»:
#   • Если в сервисе что-то пошло не так по бизнес-логике, бросайте EFHCError
#     (или его наследника), а не голый HTTPException — тогда фронт увидит
#     стабильный error_code и message.
#   • LockViolation нужен для защиты канона (per-sec ставки, запрет P2P и т.п.).
#     Его правильно бросать, когда обнаружено нарушение архитектурных правил.
#   • В main.py/приложении НЕ забудьте вызвать setup_exception_handlers(app),
#     чтобы все эти обработчики реально работали.
# =============================================================================

__all__ = [
    "EFHCError",
    "NotFoundError",
    "ValidationError",
    "BankBalanceError",
    "IdempotencyConflictError",
    "LotteryClosedError",
    "PanelsLimitExceededError",
    "ReferralError",
    "WithdrawalError",
    "TasksError",
    "normalize_exception",
    "setup_exception_handlers",
]
