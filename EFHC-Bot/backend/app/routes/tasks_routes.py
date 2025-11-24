# -*- coding: utf-8 -*-
# backend/app/routes/tasks_routes.py
# =============================================================================
# EFHC Bot — Задания (Tasks): каталог, статус пользователя, получение наград
# -----------------------------------------------------------------------------
# Канон / инварианты:
#   • Денежные операции (в т.ч. бонусы за задания) — ТОЛЬКО через банковский сервис.
#   • Все денежные POST требуют заголовок Idempotency-Key.
#   • Пользователь НИКОГДА не уходит в минус; Банк МОЖЕТ (дефицит не блокирует поток).
#   • Пагинация всех списков — строго cursor-based.
#   • Никаких суточных ставок — здесь нет генерации, только бонусы за задания.
#
# ИИ-защита:
#   • Дружелюбные ошибки и мягкие ретраи (перезагрузка страницы, повтор запроса).
#   • ETag для кэша/экономии трафика.
#   • Сервисные сбои не «роняют» процесс — фронт получает стабильные статусы.
# =============================================================================

from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config_core import get_settings
from backend.app.core.logging_core import get_logger
from backend.app.deps import (
    get_db,
    d8,
    encode_cursor,
    decode_cursor,
    make_etag,
)

# Бизнес-сервис заданий: здесь только вызовы публичных функций
from backend.app.services.tasks_service import (
    list_tasks_catalog,    # async def list_tasks_catalog(db, *, after_id: Optional[int], limit: int) -> (items, next_after_id)
    list_user_tasks,       # async def list_user_tasks(db, *, user_id: int, after_id: Optional[int], limit: int) -> (items, next_after_id)
    claim_task_reward,     # async def claim_task_reward(db, *, user_id: int, task_id: int, idempotency_key: str) -> ClaimResult
)

logger = get_logger(__name__)
settings = get_settings()
SCHEMA = getattr(settings, "DB_SCHEMA_CORE", "efhc_core")

router = APIRouter(prefix="/tasks", tags=["tasks"])

# -----------------------------------------------------------------------------
# Pydantic-схемы для ответов
# -----------------------------------------------------------------------------

class TaskItem(BaseModel):
    id: int
    code: str
    title: str
    description: Optional[str] = None
    reward_efhc: str                  # строкой с 8 знаками
    is_active: bool
    kind: Optional[str] = None        # тип/категория задания
    meta: Optional[Dict[str, Any]] = None

class CursorPage(BaseModel):
    items: List[TaskItem]
    next_cursor: Optional[str] = None
    etag: Optional[str] = None

class UserTaskItem(BaseModel):
    id: int
    task_id: int
    status: str                       # PENDING | DONE | CLAIMED | REJECTED
    progress: Optional[Dict[str, Any]] = None
    reward_efhc: str
    can_claim: bool

class UserTasksPage(BaseModel):
    items: List[UserTaskItem]
    next_cursor: Optional[str] = None
    etag: Optional[str] = None

class ClaimIn(BaseModel):
    task_id: int = Field(..., ge=1, description="ID задания для получения награды")

class ClaimOut(BaseModel):
    ok: bool
    user_id: int
    task_id: int
    reward_efhc: str
    bonus_balance: str                # новый бонусный баланс (строкой с 8 знаками)
    main_balance: str                 # новый основной баланс (строкой с 8 знаками)
    detail: str = "ok"

# -----------------------------------------------------------------------------
# Маршруты
# -----------------------------------------------------------------------------

@router.get("/catalog", response_model=CursorPage, summary="Каталог активных заданий (cursor-based)")
async def get_tasks_catalog(
    request: Request,
    response: Response,
    cursor: Optional[str] = None,
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
) -> CursorPage:
    """
    Возвращает активные задания (витрина). Пагинация — cursor-based.
    ETag: зависит от набора (items, next_cursor), чтобы экономить трафик фронтенда.
    """
    if limit < 1:
        limit = 1
    if limit > 100:
        limit = 100

    after_id: Optional[int] = None
    if cursor:
        try:
            payload = decode_cursor(cursor)
            after_id = int(payload.get("after_id"))
        except Exception:
            raise HTTPException(status_code=400, detail="Некорректный cursor")

    try:
        items_raw, next_after_id = await list_tasks_catalog(db, after_id=after_id, limit=limit)
    except Exception as e:
        logger.warning("tasks.catalog failed: %s", e)
        raise HTTPException(status_code=500, detail="Временная ошибка. Повторите попытку.")

    items: List[TaskItem] = []
    for it in items_raw:
        items.append(TaskItem(
            id=int(it["id"]),
            code=str(it["code"]),
            title=str(it["title"]),
            description=(it.get("description") if it.get("description") is not None else None),
            reward_efhc=str(d8(it["reward_efhc"])),
            is_active=bool(it.get("is_active", True)),
            kind=(it.get("kind") if it.get("kind") else None),
            meta=(it.get("meta") if it.get("meta") else None),
        ))

    next_cursor = encode_cursor({"after_id": int(next_after_id)}) if next_after_id else None

    payload_for_etag = {
        "items": [it.model_dump() for it in items],
        "next_cursor": next_cursor,
    }
    etag = make_etag(payload_for_etag)
    response.headers["ETag"] = etag

    return CursorPage(items=items, next_cursor=next_cursor, etag=etag)


@router.get("/my/{user_id}", response_model=UserTasksPage, summary="Мои задания и статусы (cursor-based)")
async def get_user_tasks(
    request: Request,
    response: Response,
    user_id: int,
    cursor: Optional[str] = None,
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
) -> UserTasksPage:
    """
    Возвращает статусы заданий для пользователя. Пагинация — cursor-based.
    Фронтенд открывает страницу — мы отдаём свежие статусы (ИИ: на стороне сервиса возможно авто-сверка).
    """
    if limit < 1:
        limit = 1
    if limit > 100:
        limit = 100

    after_id: Optional[int] = None
    if cursor:
        try:
            payload = decode_cursor(cursor)
            after_id = int(payload.get("after_id"))
        except Exception:
            raise HTTPException(status_code=400, detail="Некорректный cursor")

    try:
        items_raw, next_after_id = await list_user_tasks(db, user_id=int(user_id), after_id=after_id, limit=limit)
    except Exception as e:
        logger.warning("tasks.my failed for user %s: %s", user_id, e)
        raise HTTPException(status_code=500, detail="Временная ошибка. Повторите попытку.")

    items: List[UserTaskItem] = []
    for it in items_raw:
        items.append(UserTaskItem(
            id=int(it["id"]),
            task_id=int(it["task_id"]),
            status=str(it["status"]),
            progress=(it.get("progress") if it.get("progress") is not None else None),
            reward_efhc=str(d8(it["reward_efhc"])),
            can_claim=bool(it.get("can_claim", False)),
        ))

    next_cursor = encode_cursor({"after_id": int(next_after_id)}) if next_after_id else None
    payload_for_etag = {
        "items": [it.model_dump() for it in items],
        "next_cursor": next_cursor,
    }
    etag = make_etag(payload_for_etag)
    response.headers["ETag"] = etag

    return UserTasksPage(items=items, next_cursor=next_cursor, etag=etag)


@router.post("/claim/{user_id}", response_model=ClaimOut, summary="Получить награду за задание (только бонусы, через Банк)")
async def post_claim_task(
    user_id: int,
    payload: ClaimIn,
    db: AsyncSession = Depends(get_db),
    idempotency_key: Optional[str] = Header(default=None, convert_underscores=False, alias="Idempotency-Key"),
) -> ClaimOut:
    """
    Получение награды за задание:
      • Строго требует Idempotency-Key (денежная операция).
      • Начисление — ТОЛЬКО в bonus_balance (невыплатные монеты), через Банковский сервис.
      • Идемпотентно: повтор одного и того же Idempotency-Key не создаёт дублей.
    """
    if not idempotency_key or not idempotency_key.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Idempotency-Key header is strictly required by canon for monetary operations."
        )

    try:
        result = await claim_task_reward(
            db,
            user_id=int(user_id),
            task_id=int(payload.task_id),
            idempotency_key=idempotency_key.strip(),
        )
    except HTTPException:
        # пробрасываем уже подготовленные ошибки сервиса
        raise
    except Exception as e:
        logger.error("tasks.claim failed user=%s task=%s: %s", user_id, payload.task_id, e)
        raise HTTPException(status_code=500, detail="Временная ошибка. Повторите попытку.")

    return ClaimOut(
        ok=bool(result.ok),
        user_id=int(result.user_id),
        task_id=int(payload.task_id),
        reward_efhc=str(d8(result.reward_efhc)),
        bonus_balance=str(d8(result.user_bonus_balance)),
        main_balance=str(d8(result.user_main_balance)),
        detail=result.detail or "ok",
    )
