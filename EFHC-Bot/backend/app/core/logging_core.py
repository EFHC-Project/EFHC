# -*- coding: utf-8 -*-
# backend/app/core/logging_core.py
# =============================================================================
# Назначение кода:
#   Централизованная настройка логирования EFHC Bot:
#   • формат и хэндлеры;
#   • контекст корреляции (request_id, idempotency_key, user_id);
#   • защита от утечек секретов;
#   • удобные утилиты для модулей бота.
#
# Канон / инварианты EFHC:
#   • Единый стиль логов во всём приложении:
#       - prod — JSON (структурированные логи для агрегаторов),
#       - dev/local — человекочитаемый формат.
#   • Логи не имеют права «ронять» приложение:
#       - ошибки форматера/фильтра → мягкая деградация.
#   • Значимые операции сопровождаем полями:
#       env, svc, rid, idk, uid.
#
# ИИ-защита:
#   • Фильтр редактирует чувствительные значения (токены/ключи) в логах.
#   • Корреляция контекста через contextvars — не смешиваются запросы.
#   • При отсутствии python-json-logger автоматически откатываемся
#     к DevFormatter вместо падения.
#
# Запреты:
#   • Никакого логирования приватных данных пользователей
#     (пароли, сид-фразы, приват-ключи).
#   • Никаких сетевых/блокирующих операций в форматерах/фильтрах.
# =============================================================================

from __future__ import annotations

import contextvars
import logging
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, Mapping, MutableMapping, Optional, Tuple, Callable
from typing import Awaitable  # noqa: E401,F401 (используется в типах ASGI)

try:
    # JSON-формат логов для продакшена
    from pythonjsonlogger import jsonlogger  # type: ignore[import]

    _HAS_JSON = True
except Exception:  # pragma: no cover
    _HAS_JSON = False

from backend.app.core.config_core import get_settings

ASGIApp = Callable[
    [Mapping[str, Any], Callable[..., Awaitable[Any]], Callable[..., Awaitable[Any]]],
    Awaitable[Any],
]


# -----------------------------------------------------------------------------
# Контекст корреляции (contextvars) — безопасно для асинхронного кода
# -----------------------------------------------------------------------------
_rid_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "rid",
    default=None,
)  # request_id
_idk_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "idk",
    default=None,
)  # idempotency_key
_uid_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "uid",
    default=None,
)  # user_id (строкой, чтобы не типизировать в логах)


def set_request_context(
    *,
    request_id: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    user_id: Optional[int | str] = None,
) -> None:
    """
    Присвоить контекст корреляции текущему асинхронному потоку.

    Используется middleware и бизнес-логикой, чтобы все логи запроса
    автоматически включали request_id / idempotency_key / user_id.
    """
    if request_id is not None:
        _rid_var.set(str(request_id))
    if idempotency_key is not None:
        _idk_var.set(str(idempotency_key))
    if user_id is not None:
        _uid_var.set(str(user_id))


def clear_request_context() -> None:
    """
    Очистить контекст корреляции (после завершения запроса/таски).

    Вызывается в finally-блоках, чтобы contextvars не «текли» между задачами.
    """
    _rid_var.set(None)
    _idk_var.set(None)
    _uid_var.set(None)


# -----------------------------------------------------------------------------
# Фильтры логирования
# -----------------------------------------------------------------------------
class ContextFilter(logging.Filter):
    """
    Впрыскивает в запись логера структурированные поля из contextvars и настроек.

    Поля:
      • env  — нормализованная среда (local/dev/prod);
      • svc  — имя сервиса (PROJECT_NAME);
      • rid  — request_id (корреляция запросов);
      • idk  — idempotency_key (корреляция денежных операций);
      • uid  — user_id (если установлен).
    """

    def __init__(self, env: str, service: str) -> None:
        super().__init__()
        self._env = env
        self._svc = service

    def filter(self, record: logging.LogRecord) -> bool:
        # Не переопределяем, если уже присутствуют — оставляем возможность
        # подмены адаптерами/внешними логгерами.
        if not hasattr(record, "env"):
            record.env = self._env
        if not hasattr(record, "svc"):
            record.svc = self._svc

        rid = _rid_var.get()
        idk = _idk_var.get()
        uid = _uid_var.get()

        if not hasattr(record, "rid"):
            record.rid = rid or "-"
        if not hasattr(record, "idk"):
            record.idk = idk or "-"
        if not hasattr(record, "uid"):
            record.uid = uid or "-"

        return True


class RedactingFilter(logging.Filter):
    """
    Редактирует потенциально чувствительные значения в сообщении/параметрах,
    чтобы избежать случайной утечки секретов в логи.

    ИИ-защита:
      • Отлавливает любые исключения и не блокирует логирование.
      • Маскирует конкретные значения секретов, извлечённых из настроек,
        не полагаясь только на имена ключей.
    """

    MASK = "****"
    SECRET_KEYS: Tuple[str, ...] = (
        "TELEGRAM_BOT_TOKEN",
        "SECRET_KEY",
        "DATABASE_URL",
        "SUPABASE_KEY",
        "TON_API_KEY",
        "HMAC_SECRET",
    )

    def __init__(self, settings_obj: object) -> None:
        super().__init__()
        self._secrets: list[str] = []
        # Извлекаем реальные значения из настроек (если присутствуют).
        for key in self.SECRET_KEYS:
            try:
                val = getattr(settings_obj, key, None)  # type: ignore[arg-type]
                if val and isinstance(val, str):
                    self._secrets.append(val)
            except Exception:
                # Мягкая деградация — лучше не паниковать из-за кривых настроек.
                continue

    def _redact_text(self, text: str) -> str:
        if not text:
            return text
        redacted = text
        for secret in self._secrets:
            if secret and secret in redacted:
                redacted = redacted.replace(secret, self.MASK)
        return redacted

    def filter(self, record: logging.LogRecord) -> bool:  # type: ignore[override]
        # Редактируем только message/args; extra-поля не трогаем —
        # jsonlogger сам сериализует их.
        try:
            if isinstance(record.msg, str):
                record.msg = self._redact_text(record.msg)

            if isinstance(record.args, tuple):
                record.args = tuple(
                    self._redact_text(str(arg)) for arg in record.args
                )
        except Exception:
            # Ни при каких обстоятельствах фильтр не должен ломать логирование.
            pass
        return True


# -----------------------------------------------------------------------------
# Форматеры
# -----------------------------------------------------------------------------
class DevFormatter(logging.Formatter):
    """
    Человекочитаемый формат для local/dev-окружений.

    Пример строки:
    2025-11-22 12:00:00 | INFO     | EFHC Bot | backend | rid=... idk=... uid=... | msg
    """

    def __init__(self) -> None:
        super().__init__(
            fmt=(
                "%(asctime)s | %(levelname)-8s | %(svc)s | %(name)s | "
                "rid=%(rid)s idk=%(idk)s uid=%(uid)s | %(message)s"
            ),
            datefmt="%Y-%m-%d %H:%M:%S",
        )


def _make_json_formatter() -> logging.Formatter:
    """
    Создаёт JSON-форматер для продакшн-окружения.

    Структура JSON:
        {
          "time": "...",
          "level": "INFO",
          "service": "EFHC Bot",
          "logger": "backend.app.module",
          "env": "prod",
          "rid": "...",
          "idk": "...",
          "uid": "...",
          "msg": "Текст сообщения"
        }

    При отсутствии python-json-logger откатываемся к DevFormatter.
    """
    if not _HAS_JSON:
        return DevFormatter()

    fmt = (
        "%(asctime)s "
        "%(levelname)s "
        "%(svc)s "
        "%(name)s "
        "%(env)s "
        "%(rid)s "
        "%(idk)s "
        "%(uid)s "
        "%(message)s"
    )

    class JsonFormatter(jsonlogger.JsonFormatter):
        def process_log_record(
            self,
            record: Dict[str, Any],
        ) -> Dict[str, Any]:
            base = super().process_log_record(record)
            return {
                "time": base.get("asctime"),
                "level": base.get("levelname"),
                "service": base.get("svc"),
                "logger": base.get("name"),
                "env": base.get("env"),
                "rid": base.get("rid"),
                "idk": base.get("idk"),
                "uid": base.get("uid"),
                "msg": base.get("message"),
            }

    return JsonFormatter(fmt=fmt)


# -----------------------------------------------------------------------------
# Инициализация логирования
# -----------------------------------------------------------------------------
def setup_logging() -> None:
    """
    Полностью настраивает логирование:

      • root-логгер, формат, уровни;
      • консоль (stdout) и файл (в local);
      • фильтры контекста и редактирования;
      • uvicorn/fastapi-логгеры → в root (единый формат);
      • SQLAlchemy-логгер в режиме DEBUG.

    ИИ-защита:
      • Любые ошибки при настройке форматеров/фильтров не должны ломать
        приложение; в худшем случае — останемся с базовой конфигурацией.
    """
    settings = get_settings()
    env = settings.env_normalized  # "local" / "dev" / "prod"
    debug = bool(getattr(settings, "DEBUG", False))
    service = getattr(settings, "PROJECT_NAME", "EFHC Bot")

    root = logging.getLogger()
    root.handlers.clear()
    level = logging.DEBUG if debug else logging.INFO
    root.setLevel(level)

    ctx_filter = ContextFilter(env=env, service=service)
    redact_filter = RedactingFilter(settings_obj=settings)

    # --- Консольный хэндлер ---
    console_handler = logging.StreamHandler(sys.stdout)
    if env in ("local", "dev"):
        formatter: logging.Formatter = DevFormatter()
    else:
        formatter = _make_json_formatter()
    console_handler.setFormatter(formatter)
    console_handler.addFilter(ctx_filter)
    console_handler.addFilter(redact_filter)
    root.addHandler(console_handler)

    # --- Локальный файл логов (только local) ---
    if env == "local":
        logs_dir = Path(".local_artifacts") / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(
            logs_dir / "app.log",
            encoding="utf-8",
        )
        file_handler.setFormatter(DevFormatter())
        file_handler.addFilter(ctx_filter)
        file_handler.addFilter(redact_filter)
        root.addHandler(file_handler)

    # --- Перехват uvicorn/fastapi ---
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "fastapi"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.setLevel(level)
        logger.propagate = True  # всё идёт в root с нашими фильтрами

    # --- SQLAlchemy (минимальный уровень шума) ---
    if debug:
        sqlalchemy_logger = logging.getLogger("sqlalchemy.engine")
        sqlalchemy_logger.setLevel(logging.INFO)

    # Финальная запись об инициализации
    logging.getLogger(__name__).info(
        "Logging initialized",
        extra={
            "details": {
                "env": env,
                "debug": debug,
                "level": logging.getLevelName(level),
            },
        },
    )


def get_logger(name: Optional[str] = None, **extra: Any) -> logging.Logger:
    """
    Получить логгер по имени и (опционально) привязать дополнительные поля
    через LoggerAdapter.

    Пример:
        log = get_logger(__name__, component="watcher")
        log.info("начал обработку", extra={"step": "fetch"})
    """
    base = logging.getLogger(name)
    if not extra:
        return base
    return logging.LoggerAdapter(base, extra)  # type: ignore[return-value]


def logger_with(logger: logging.Logger, **extra: Any) -> logging.Logger:
    """
    Обернуть существующий логгер адаптером с дополнительными полями.

    Удобно в сервисах для «поточного» расширения контекста:
        log = logger_with(log, panel_id=panel.id)
    """
    return logging.LoggerAdapter(logger, extra)  # type: ignore[return-value]


# -----------------------------------------------------------------------------
# ASGI-middleware для корреляции (подключается в main.py)
# -----------------------------------------------------------------------------
class CorrelationIdMiddleware:
    """
    Впрыскивает X-Request-ID и Idempotency-Key из HTTP-заголовков в contextvars,
    чтобы все логи запроса автоматически содержали rid/idk/uid.

    Правила:
      • Если X-Request-ID отсутствует — генерируется UUID4 (hex).
      • user_id в этом слое не извлекается (аутентификация ещё не выполнена);
        сервисы могут позднее вызвать set_request_context(user_id=...).
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(
        self,
        scope: Mapping[str, Any],
        receive: Callable[..., Awaitable[Any]],
        send: Callable[..., Awaitable[Any]],
    ) -> None:
        if scope.get("type") != "http":  # пропускаем non-HTTP события
            await self.app(scope, receive, send)
            return

        # Приводим заголовки к dict[str, str]
        raw_headers: MutableMapping[bytes, bytes] = dict(
            scope.get("headers") or [],
        )
        headers: Dict[str, str] = {
            key.decode().lower(): value.decode()
            for key, value in raw_headers.items()
        }

        rid = headers.get("x-request-id") or uuid.uuid4().hex
        idk = headers.get("idempotency-key")

        set_request_context(request_id=rid, idempotency_key=idk)

        async def send_wrapper(message: Mapping[str, Any]) -> None:
            if message.get("type") == "http.response.start":
                headers_list: list[Tuple[bytes, bytes]] = list(
                    message.get("headers") or [],
                )
                headers_list.append((b"x-request-id", rid.encode("utf-8")))
                new_message: Dict[str, Any] = dict(message)
                new_message["headers"] = headers_list
                await send(new_message)
                return

            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            clear_request_context()


# -----------------------------------------------------------------------------
# Автоконфигурация при импорте (по канону EFHC)
# -----------------------------------------------------------------------------
setup_logging()

__all__ = [
    "setup_logging",
    "get_logger",
    "logger_with",
    "set_request_context",
    "clear_request_context",
    "CorrelationIdMiddleware",
]
# =============================================================================
# Пояснения «для чайника»:
#   • Этот модуль отвечает за единый стиль логов и безопасный контекст
#     корреляции во всём EFHC Bot.
#   • В dev/local вы увидите читаемые строки; в prod — структурированный JSON,
#     готовый для централизованного сбора (по ключам env/rid/idk/uid).
#   • Подключите CorrelationIdMiddleware в main.py, чтобы каждая HTTP-ручка
#     автоматически получала и возвращала уникальный X-Request-ID.
#   • Секреты (токены/ключи) в логах автоматически редактируются как "****".
# =============================================================================
