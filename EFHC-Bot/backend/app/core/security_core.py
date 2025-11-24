# -*- coding: utf-8 -*-
# backend/app/core/security_core.py
# =============================================================================
# Назначение кода:
#   Централизованный слой безопасности EFHC Bot:
#   • JWT (HS256) + exp/jti;
#   • ролевые проверки админов (по JWT и банковскому Telegram ID);
#   • валидация Telegram WebApp initData;
#   • серверный X-Admin-Api-Key;
#   • мини-комбинатор Depends: one_of(...).
#
# Канон / инварианты EFHC:
#   • Здесь НЕТ денежных операций и балансов — только «кто ты» и «можно/нельзя».
#   • Админка по эмиссии/сжиганию/банку доступна только:
#       - по роли ADMIN в JWT,
#       - по ADMIN_BANK_TELEGRAM_ID,
#       - по валидному X-Admin-Api-Key.
#   • JWT: алгоритм HS256, обязательный exp; SECRET_KEY берём из настроек.
#   • Telegram WebApp initData валидируется строго по алгоритму Telegram
#     (HMAC-SHA256 от data-check-string).
#
# ИИ-защита / самовосстановление:
#   • Любые ошибки в безопасности → контролируемые 401/403/400, а не падение
#     процесса FastAPI.
#   • TTL initData читается из настроек, есть безопасный дефолт (600 сек).
#   • Альтернативные гварды (JWT / банк-ID / API-ключ) повышают отказоустойчивость
#     при временных сбоях одного из механизмов.
#
# Запреты:
#   • Никаких правок балансов/денег в этом модуле.
#   • Никакой бизнес-логики — только аутентификация/авторизация.
# =============================================================================

from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, Optional
from urllib.parse import parse_qsl
from uuid import uuid4

import jwt
from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from passlib.context import CryptContext

from backend.app.core.config_core import get_settings
from backend.app.core.logging_core import get_logger

logger = get_logger(__name__)
settings = get_settings()

# -----------------------------------------------------------------------------
# Базовые контексты / константы безопасности
# -----------------------------------------------------------------------------
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
auth_scheme = HTTPBearer(auto_error=False)

JWT_ALGORITHM = "HS256"

DEFAULT_ACCESS_TTL_MIN = int(
    getattr(settings, "ACCESS_TOKEN_EXPIRE_MINUTES", 1440) or 1440,
)

# TTL проверки Telegram WebApp initData (секунды). По умолчанию 10 минут.
WEBAPP_INITDATA_TTL_SEC = int(
    getattr(settings, "WEBAPP_INITDATA_TTL_SEC", 600) or 600,
)

# Серверный админ-ключ (опционально).
# Если не задан — остаются только JWT/банк-ID.
ADMIN_API_KEY: Optional[str] = getattr(settings, "ADMIN_API_KEY", None)

# Банковский админ — Telegram ID из .env (строго int, иначе None).
try:
    ADMIN_BANK_TELEGRAM_ID: Optional[int] = int(
        getattr(settings, "ADMIN_BANK_TELEGRAM_ID"),
    )
except Exception:  # noqa: BLE001
    ADMIN_BANK_TELEGRAM_ID = None

Payload = Dict[str, Any]
Guard = Callable[..., Awaitable[Any]]


# -----------------------------------------------------------------------------
# Пароли (для возможных локальных аккаунтов админки)
# -----------------------------------------------------------------------------
def hash_password(password: str) -> str:
    """Хешируем пароль (bcrypt)."""
    return pwd_context.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    """Проверяем пароль (bcrypt)."""
    return pwd_context.verify(password, hashed)


# -----------------------------------------------------------------------------
# JWT токены (HS256)
# -----------------------------------------------------------------------------
def _get_secret_key() -> str:
    """
    Возвращает SECRET_KEY из настроек или поднимает 500,
    если секрет не сконфигурирован.

    Это соответствует канону: никаких «фиктивных» секретов по умолчанию.
    """
    key = getattr(settings, "SECRET_KEY", None)
    if not key:
        logger.error("SECRET_KEY is not configured")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Server is not configured",
        )
    return str(key)


def create_jwt_token(
    subject: str,
    *,
    expires_delta: Optional[timedelta] = None,
    extra: Optional[Payload] = None,
) -> str:
    """
    Создаёт JWT с полями:
      • sub — строковый идентификатор (внутренний user_id или telegram_id);
      • iat — момент выпуска (UTC);
      • exp — момент истечения (UTC);
      • jti — уникальный идентификатор токена;
      • extra — произвольные дополнительные поля (role, tg_id и т.п.).
    """
    if expires_delta is None:
        expires_delta = timedelta(minutes=DEFAULT_ACCESS_TTL_MIN)

    now = datetime.now(tz=timezone.utc)
    payload: Payload = {
        "sub": subject,
        "iat": int(now.timestamp()),
        "exp": int((now + expires_delta).timestamp()),
        "jti": uuid4().hex,
    }
    if extra:
        payload.update(extra)

    secret_key = _get_secret_key()
    token = jwt.encode(payload, secret_key, algorithm=JWT_ALGORITHM)
    return token


def decode_jwt_token(token: str) -> Payload:
    """
    Декод JWT с контролируемыми ошибками (401).

    Любая проблема верификации токена оборачивается в HTTPException 401,
    чтобы не раскрывать детали внутренней реализации.
    """
    secret_key = _get_secret_key()
    try:
        return jwt.decode(
            token,
            secret_key,
            algorithms=[JWT_ALGORITHM],
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token expired",
        )
    except jwt.InvalidTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
        )


# -----------------------------------------------------------------------------
# Depends: текущий пользователь и админ-гварды
# -----------------------------------------------------------------------------
async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(auth_scheme),
) -> Payload:
    """
    Стандартный Bearer-путь аутентификации. Возвращает payload токена.

    401 — если токена нет или он некорректен.
    """
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )
    return decode_jwt_token(credentials.credentials)


def _is_bank_admin(payload: Payload) -> bool:
    """
    Признаём субъекта админом, если выполнено хотя бы одно:

      • payload["role"] == "ADMIN";
      • payload["tg_id"] совпадает с ADMIN_BANK_TELEGRAM_ID.

    Это связывает JWT-аутентификацию с банковским Telegram ID,
    указанным в .env.
    """
    if payload.get("role") == "ADMIN":
        return True

    tg_id = payload.get("tg_id")
    if ADMIN_BANK_TELEGRAM_ID is not None and tg_id is not None:
        try:
            return int(tg_id) == int(ADMIN_BANK_TELEGRAM_ID)
        except Exception:  # noqa: BLE001
            logger.warning("Failed to cast tg_id to int in JWT payload")
            return False

    return False


async def get_current_admin(
    user: Payload = Depends(get_current_user),
) -> Payload:
    """
    Гвард для админ-ручек по JWT / банковскому Telegram ID.

    403 — если прав недостаточно.
    """
    if not _is_bank_admin(user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not enough rights",
        )
    return user


async def get_admin_api_key_guard(
    x_admin_api_key: Optional[str] = Header(
        default=None,
        convert_underscores=False,
        alias="X-Admin-Api-Key",
    ),
) -> str:
    """
    Альтернативный серверный допуск: валидный X-Admin-Api-Key.

    Удобен для автоматизаций/скриптов/планировщика. Если ключ отсутствует
    или не совпадает — 401.
    """
    if ADMIN_API_KEY and x_admin_api_key:
        if hmac.compare_digest(ADMIN_API_KEY, x_admin_api_key):
            return x_admin_api_key

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Admin API key required",
    )


# -----------------------------------------------------------------------------
# Мини-Depends: комбинатор «любой из гардов»
# -----------------------------------------------------------------------------
def one_of(*guards: Guard) -> Guard:
    """
    Комбинатор FastAPI-зависимостей: пропускает, если успешно прошёл
    ХОТЯ БЫ ОДИН guard.

    Если все провалились — прокидывает последнюю причину (обычно 401/403).

    Пример:
        require_admin_or_key = one_of(
            get_current_admin,
            get_admin_api_key_guard,
        )
        # в роуте: _auth = Depends(require_admin_or_key)
    """

    async def _combined(**kwargs: Any) -> Any:
        last_exc: Optional[HTTPException] = None
        for guard in guards:
            try:
                return await guard(**kwargs)
            except HTTPException as exc:
                last_exc = exc
                continue

        raise last_exc or HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )

    return _combined


# Готовая мини-зависимость для админ-ручек:
# «админ по JWT/банк-ID» ИЛИ «X-Admin-Api-Key».
require_admin_or_key: Guard = one_of(
    get_current_admin,
    get_admin_api_key_guard,
)


# -----------------------------------------------------------------------------
# Telegram WebApp initData validation
# -----------------------------------------------------------------------------
def _telegram_data_check_string(init_items: Dict[str, str]) -> str:
    """
    Формируем data-check-string: пары key=value, отсортированные по ключу,
    разделённые символом перевода строки.

    Поле 'hash' исключается (по спецификации Telegram).
    """
    pairs = [
        f"{k}={v}"
        for k, v in sorted(init_items.items())
        if k != "hash"
    ]
    return "\n".join(pairs)


def validate_telegram_init_data(init_data: str) -> Dict[str, Any]:
    """
    Верификация подписи Telegram WebApp initData.

    Алгоритм:
      1) Разобрать init_data как query-string (URL-декод).
      2) Сформировать data-check-string из отсортированных пар k=v (без 'hash').
      3) secret_key = sha256(TELEGRAM_BOT_TOKEN.encode()).
      4) calc_hash = HMAC_SHA256(secret_key, data-check-string).hexdigest().
      5) Сравнить с переданным 'hash'.
      6) Проверить TTL по 'auth_date' (по умолчанию 600 сек).

    Возвращает распарсенный словарь:
      • все поля, переданные Telegram;
      • поле user — уже JSON-объект, если его удалось распарсить.
    """
    parsed_pairs: Dict[str, str] = dict(
        parse_qsl(
            init_data,
            keep_blank_values=True,
            strict_parsing=False,
        ),
    )

    if "hash" not in parsed_pairs:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing hash in initData",
        )

    received_hash = parsed_pairs.get("hash") or ""
    if not received_hash:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Empty hash in initData",
        )

    check_string = _telegram_data_check_string(parsed_pairs)

    bot_token = getattr(settings, "TELEGRAM_BOT_TOKEN", None)
    if not bot_token:
        logger.error("TELEGRAM_BOT_TOKEN is not configured")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Server is not configured",
        )

    secret_key = hashlib.sha256(bot_token.encode("utf-8")).digest()
    calc_hash = hmac.new(
        secret_key,
        check_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(received_hash, calc_hash):
        logger.warning("Invalid Telegram initData signature")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Telegram signature",
        )

    # TTL по auth_date (если присутствует).
    auth_date = parsed_pairs.get("auth_date")
    if auth_date and auth_date.isdigit():
        now = int(time.time())
        if now - int(auth_date) > WEBAPP_INITDATA_TTL_SEC:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="initData is too old",
            )

    # Поле user — JSON-строка → объект (если парсится).
    user_raw = parsed_pairs.get("user")
    if user_raw:
        try:
            parsed_pairs["user"] = json.loads(user_raw)
        except Exception:  # noqa: BLE001
            logger.warning("initData 'user' field is not a valid JSON")

    return parsed_pairs


__all__ = [
    "hash_password",
    "verify_password",
    "create_jwt_token",
    "decode_jwt_token",
    "get_current_user",
    "get_current_admin",
    "get_admin_api_key_guard",
    "one_of",
    "require_admin_or_key",
    "validate_telegram_init_data",
]

# =============================================================================
# Пояснения «для чайника»:
#   • Этот модуль отвечает только за безопасность (кто и с какими правами),
#     но НЕ трогает деньги и балансы.
#   • `require_admin_or_key` убирает дублирование проверок в роутерах и
#     делает админ-ручки устойчивыми к временным проблемам JWT-сессий
#     или серверного API-ключа.
#   • Любая логика идемпотентности и денежные инварианты EFHC реализуются
#     в сервисах/банк-сервисах, а не здесь.
#   • Для фоновых задач планировщика рекомендуется при старте тика
#     устанавливать request_id через logging_core.set_request_context(...),
#     чтобы логи были связаны по rid.
# =============================================================================
