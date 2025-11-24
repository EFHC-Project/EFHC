# -*- coding: utf-8 -*-
# backend/app/deps.py
# =============================================================================
# EFHC Bot — Общие зависимости FastAPI: БД-сессия, идемпотентность, аутентификация,
#             админ-гейт, keyset-пагинация, ETag и точность Decimal.
# -----------------------------------------------------------------------------
# Канон/требования:
#   • Все денежные POST — строго с Idempotency-Key (или client_nonce).
#   • Списки — только cursor-based (keyset) пагинация.
#   • Источник истины для генерации — посекундные ставки (в сервисах; тут не считаем).
#   • Пользователь не может уходить в минус, Банк — может (проверки в сервисах/locks).
#
# Этот модуль НЕ делает бизнес-логику, только инфраструктуру/валидацию.
# =============================================================================
from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Any, Dict, Optional, Tuple

from fastapi import Depends, Header, HTTPException, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config_core import get_settings
from backend.app.core.logging_core import get_logger
from backend.app.core.database_core import lifespan_session

logger = get_logger(__name__)
settings = get_settings()


async def get_db() -> AsyncSession:
    """
    Выдаёт AsyncSession для роутов/сервисов.
    • Роут/сервис сам решает — коммитить или нет (паттерн unit of work).
    • При исключении в стеке зависимостей — безопасный rollback().
    """
    async with lifespan_session() as session:
        yield session


# -----------------------------------------------------------------------------
# Точность Decimal и утилиты для UI/списков
# -----------------------------------------------------------------------------
EFHC_DECIMALS: int = int(getattr(settings, "EFHC_DECIMALS", 8) or 8)
Q8 = Decimal(1).scaleb(-EFHC_DECIMALS)


def d8(x: Any) -> Decimal:
    """Округление вниз до 8 знаков — строго по канону."""
    return Decimal(str(x)).quantize(Q8, rounding=ROUND_DOWN)


def make_etag(payload: Dict[str, Any]) -> str:
    """
    Делает детерминированный ETag из JSON-представления payload.
    Используется фронтом для «304 Not Modified».
    """
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def encode_cursor(ts: datetime, row_id: int) -> str:
    """
    Keyset-cursor b64(ts|id), где ts — unix-timestamp (UTC), id — целочисленный PK.
    """
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    blob = f"{int(ts.timestamp())}|{row_id}".encode("utf-8")
    return base64.urlsafe_b64encode(blob).decode("ascii")


def decode_cursor(cursor: str) -> Tuple[int, int]:
    """
    Инверсия encode_cursor: возвращает (ts_unix:int, row_id:int).
    Бросает HTTP 400 при некорректной строке.
    """
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
        ts_str, id_str = raw.split("|", 1)
        return int(ts_str), int(id_str)
    except Exception:
        raise HTTPException(status_code=400, detail="Некорректный cursor")


# -----------------------------------------------------------------------------
# Идемпотентность денежных операций
# -----------------------------------------------------------------------------

def normalize_idempotency_key(raw: Optional[str]) -> str:
    """
    Нормализует произвольную строку пользователя в безопасный ключ.
    Удобно, когда client_nonce приходит в body — роут может сам вызвать эту функцию.
    """
    if not raw or not raw.strip():
        raise HTTPException(status_code=400, detail="Idempotency-Key (или client_nonce) обязателен")
    return hashlib.sha256(raw.strip().encode("utf-8")).hexdigest()


async def require_idempotency_key(
    idempotency_key: Optional[str] = Header(default=None, convert_underscores=False, alias="Idempotency-Key"),
) -> str:
    """
    Depend для денежных POST/PUT/PATCH/DELETE: требует Idempotency-Key.
    """
    if not idempotency_key:
        raise HTTPException(
            status_code=400,
            detail="Idempotency-Key header is strictly required for monetary operations.",
        )
    return idempotency_key.strip()


# -----------------------------------------------------------------------------
# Аутентификация/админ-гейт (минимальный, т.к. бот управляет токенами сам)
# -----------------------------------------------------------------------------
@dataclass
class AuthContext:
    user_id: Optional[int]
    telegram_id: Optional[int]
    is_admin: bool = False


def get_auth_context(request: Request) -> AuthContext:
    """
    Простейший auth-контекст из заголовков (для совместимости с фронтом/ботом).
    В реальном проде это будет JWT/сессия. Здесь — «доверенный» слой API/бота.
    """
    tg_id_raw = request.headers.get("X-Telegram-Id")
    user_id_raw = request.headers.get("X-User-Id")
    is_admin_raw = request.headers.get("X-Admin")
    try:
        tg_id = int(tg_id_raw) if tg_id_raw else None
    except Exception:
        tg_id = None
    try:
        user_id = int(user_id_raw) if user_id_raw else None
    except Exception:
        user_id = None
    is_admin = str(is_admin_raw).lower() == "true" if is_admin_raw is not None else False
    return AuthContext(user_id=user_id, telegram_id=tg_id, is_admin=is_admin)


async def require_admin(ctx: AuthContext = Depends(get_auth_context)) -> AuthContext:
    if not ctx.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    return ctx


async def require_user(ctx: AuthContext = Depends(get_auth_context)) -> AuthContext:
    if ctx.user_id is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User authentication required")
    return ctx


# -----------------------------------------------------------------------------
# Пагинация (query-параметры)
# -----------------------------------------------------------------------------

async def pagination_params(
    cursor: Optional[str] = Query(None, description="Keyset cursor b64(ts|id)"),
    limit: int = Query(50, ge=1, le=500, description="Page size (1..500)"),
) -> Dict[str, Any]:
    """
    Раскодирует курсор и лимит в удобный словарь.
    Возвращает dict: {"cursor": cursor, "limit": limit, "ts": ts, "id": row_id}
    """
    if cursor:
        ts_unix, row_id = decode_cursor(cursor)
        return {"cursor": cursor, "limit": limit, "ts": ts_unix, "id": row_id}
    return {"cursor": None, "limit": limit, "ts": None, "id": None}


# -----------------------------------------------------------------------------
# ETag helper
# -----------------------------------------------------------------------------

def etag(payload: Dict[str, Any]) -> str:
    return make_etag(payload)


__all__ = [
    "AuthContext",
    "d8",
    "make_etag",
    "encode_cursor",
    "decode_cursor",
    "normalize_idempotency_key",
    "require_idempotency_key",
    "get_db",
    "require_admin",
    "require_user",
    "get_auth_context",
    "pagination_params",
    "etag",
]
