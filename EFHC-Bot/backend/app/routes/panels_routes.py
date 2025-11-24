# -*- coding: utf-8 -*-
# backend/app/routes/panels_routes.py
# =============================================================================
# EFHC Bot — Роуты панелей (витрины Active/Archive, сводка, покупка)
# -----------------------------------------------------------------------------
# Назначение (коротко, 1–3 строки):
#   • Отдаёт списки активных/архивных панелей с курсорной пагинацией + ETag,
#     сводку для экрана Panels, и безопасную покупку панелей за EFHC.
#
# Канон/инварианты (строго):
#   • Покупка панели — 100 EFHC. Списание строго: СНАЧАЛА bonus, затем main.
#   • Пользователь НИКОГДА не уходит в минус (жёсткий запрет). Банк может — это допустимо.
#   • Генерация везде посекундная (GEN_PER_SEC_BASE_KWH/GEN_PER_SEC_VIP_KWH). Никаких «суточных» API.
#   • Любые денежные POST обязаны требовать заголовок Idempotency-Key (финансовая целостность).
#   • P2P запрещён. EFHC→kWh запрещено. NFT — только заявка (не относится к этим ручкам).
#
# ИИ-защиты/самовосстановление:
#   • «Принудительная синхронизация» по запросу фронтенда: при открытии раздела
#     можно передать force_sync=1, и бэкенд выполнит догон энергии для пользователя
#     перед формированием ответа (не ломает общий цикл, уменьшает дрейф UI).
#   • Курсорная пагинация + ETag: фронтенд может экономить трафик, сравнивая ETag.
#   • Дружелюбные, детерминированные ошибки с логированием, без «падений».
#
# Запреты:
#   • Никаких прямых изменений балансов/эмиссии — только через банковский сервис из panels_service.
#   • Никаких «суточных» значений в ответах.
# =============================================================================

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config_core import get_settings
from backend.app.core.logging_core import get_logger
from backend.app.deps import get_db, d8, etag  # централизованные утилиты: округление/ETag
from backend.app.services import panels_service as svc
from backend.app.services.energy_service import backfill_user  # «принудительный догон» по пользователю

logger = get_logger(__name__)
settings = get_settings()

router = APIRouter(prefix="/panels", tags=["panels"])

# -----------------------------------------------------------------------------
# Pydantic-схемы
# -----------------------------------------------------------------------------

class PanelItemOut(BaseModel):
    id: int
    created_at: Optional[str] = None
    expires_at: Optional[str] = None
    archived_at: Optional[str] = None
    base_gen_per_sec: str
    generated_kwh: str
    is_active: bool

class PanelsListOut(BaseModel):
    items: List[PanelItemOut]
    next_cursor: Optional[str] = Field(None, description="Курсор для следующей страницы или null")

class PanelsSummaryOut(BaseModel):
    active_count: int
    total_generated_by_panels: str
    nearest_expire_at: Optional[str] = None

class PurchasePanelIn(BaseModel):
    user_id: int = Field(..., description="ID пользователя (внутренний)")
    qty: int = Field(1, ge=1, le=1000, description="Сколько панелей купить (>=1)")

class PurchasePanelOut(BaseModel):
    ok: bool
    qty: int
    total_spent_bonus: str
    total_spent_main: str
    created_panel_ids: List[int]
    detail: str

# -----------------------------------------------------------------------------
# GET /panels/{user_id}/summary  — короткая сводка для экрана Panels
# -----------------------------------------------------------------------------
@router.get("/{user_id}/summary", response_model=PanelsSummaryOut, summary="Сводка по панелям")
async def get_panels_summary(
    user_id: int,
    force_sync: Optional[int] = 0,
    db: AsyncSession = Depends(get_db),
) -> PanelsSummaryOut:
    """
    Что делает:
      • Возвращает: active_count, total_generated_by_panels, nearest_expire_at.
      • Если force_sync=1 — сперва выполняет «догон» энергии для пользователя.

    Побочные эффекты:
      • При force_sync=1 может обновить энергию пользователя (safe, идемпотентно).

    Исключения:
      • 500 — если подсистемы временно недоступны (логируются).
    """
    try:
        if force_sync:
            # «Принудительная синхронизация» по запросу UI (не обязательна, но снижает дрейф)
            await backfill_user(db, user_id=user_id)

        s = await svc.panels_summary(db, user_id=user_id)
        return PanelsSummaryOut(
            active_count=int(s.active_count),
            total_generated_by_panels=str(d8(s.total_generated_by_panels)),
            nearest_expire_at=(s.nearest_expire_at.isoformat() if s.nearest_expire_at else None),
        )
    except Exception as e:
        logger.exception("panels.summary failed: %s", e)
        raise HTTPException(status_code=500, detail="Временная ошибка. Повторите попытку позже.")

# -----------------------------------------------------------------------------
# GET /panels/{user_id}/active  — активные панели (курсорная пагинация + ETag)
# -----------------------------------------------------------------------------
@router.get("/{user_id}/active", response_model=PanelsListOut, summary="Активные панели (курсорная пагинация)")
async def list_active_panels(
    user_id: int,
    limit: int = 25,
    cursor: Optional[str] = None,
    force_sync: Optional[int] = 0,
    response: Response = None,
    db: AsyncSession = Depends(get_db),
) -> PanelsListOut:
    """
    Что делает:
      • Возвращает список активных панелей пользователя (is_active = TRUE) с курсорной пагинацией.
      • Отдаёт ETag в заголовке ответа для экономии трафика.
      • Если force_sync=1 — перед этим выполняет «догон» энергии пользователя.

    Вход:
      • user_id — ID пользователя (внутренний)
      • limit — от 1 до 100
      • cursor — курсор предыдущей страницы
      • force_sync — 1 для принудительной синхронизации энергии

    Исключения:
      • 500 — временная ошибка.
    """
    try:
        if force_sync:
            await backfill_user(db, user_id=user_id)

        page = await svc.list_active_panels(db, user_id=user_id, limit=limit, cursor=cursor)
        items_out = [
            PanelItemOut(
                id=int(i["id"]),
                created_at=i.get("created_at"),
                expires_at=i.get("expires_at"),
                archived_at=i.get("archived_at"),
                base_gen_per_sec=str(i["base_gen_per_sec"]),
                generated_kwh=str(i["generated_kwh"]),
                is_active=bool(i["is_active"]),
            )
            for i in page.items
        ]
        payload = PanelsListOut(items=items_out, next_cursor=page.next_cursor)

        # Стабильный ETag (по id и количеству + next_cursor); фронту легче кэшировать
        e = etag({
            "kind": "panels_active",
            "user_id": int(user_id),
            "ids": [int(i.id) for i in items_out],
            "next": page.next_cursor or "",
        })
        if response is not None:
            response.headers["ETag"] = e

        return payload
    except Exception as e:
        logger.exception("panels.active failed: %s", e)
        raise HTTPException(status_code=500, detail="Временная ошибка. Повторите попытку позже.")

# -----------------------------------------------------------------------------
# GET /panels/{user_id}/archive  — архивные панели (курсорная пагинация + ETag)
# -----------------------------------------------------------------------------
@router.get("/{user_id}/archive", response_model=PanelsListOut, summary="Архивные панели (курсорная пагинация)")
async def list_archived_panels(
    user_id: int,
    limit: int = 25,
    cursor: Optional[str] = None,
    force_sync: Optional[int] = 0,
    response: Response = None,
    db: AsyncSession = Depends(get_db),
) -> PanelsListOut:
    """
    Что делает:
      • Возвращает список архивных панелей пользователя (is_active = FALSE) с курсорной пагинацией.
      • Отдаёт ETag в заголовке ответа.
      • Если force_sync=1 — выполняет «догон» энергии (на случай, если архивирование произошло «впритык»).

    Исключения:
      • 500 — временная ошибка.
    """
    try:
        if force_sync:
            await backfill_user(db, user_id=user_id)

        page = await svc.list_archived_panels(db, user_id=user_id, limit=limit, cursor=cursor)
        items_out = [
            PanelItemOut(
                id=int(i["id"]),
                created_at=i.get("created_at"),
                expires_at=i.get("expires_at"),
                archived_at=i.get("archived_at"),
                base_gen_per_sec=str(i["base_gen_per_sec"]),
                generated_kwh=str(i["generated_kwh"]),
                is_active=bool(i["is_active"]),
            )
            for i in page.items
        ]
        payload = PanelsListOut(items=items_out, next_cursor=page.next_cursor)

        e = etag({
            "kind": "panels_archived",
            "user_id": int(user_id),
            "ids": [int(i.id) for i in items_out],
            "next": page.next_cursor or "",
        })
        if response is not None:
            response.headers["ETag"] = e

        return payload
    except Exception as e:
        logger.exception("panels.archive failed: %s", e)
        raise HTTPException(status_code=500, detail="Временная ошибка. Повторите попытку позже.")

# -----------------------------------------------------------------------------
# POST /panels/purchase  — покупка панелей (денежная операция, нужен Idempotency-Key)
# -----------------------------------------------------------------------------
@router.post("/purchase", response_model=PurchasePanelOut, summary="Покупка панелей (Idempotency-Key обязателен)")
async def purchase_panels(
    data: PurchasePanelIn,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db),
) -> PurchasePanelOut:
    """
    Что делает:
      • Списывает EFHC у пользователя (сначала бонус, потом основной) и создаёт записи панелей.
      • Идемпотентность — через заголовок Idempotency-Key (обязателен).

    Вход:
      • body: { user_id, qty }
      • headers: Idempotency-Key: <строка>

    Побочные эффекты:
      • Денежные движения оформляются банковским сервисом, зеркально отражаются в логах.
      • Создаются новые записи активных панелей (срок 180 дней).

    Исключения:
      • 400 — отсутствует Idempotency-Key / неверные входные данные.
      • 403 — пользователь в историческом минусе (покупки за EFHC запрещены).
      • 409 — превышен лимит панелей.
      • 500 — прочие временные ошибки.
    """
    if not idempotency_key or not idempotency_key.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Idempotency-Key header is strictly required by canon for monetary operations."
        )

    try:
        res = await svc.purchase_panel(
            db,
            user_id=int(data.user_id),
            qty=int(data.qty),
            idempotency_key=idempotency_key.strip(),
        )
        return PurchasePanelOut(
            ok=bool(res.ok),
            qty=int(res.qty),
            total_spent_bonus=str(d8(res.total_spent_bonus)),
            total_spent_main=str(d8(res.total_spent_main)),
            created_panel_ids=[int(x) for x in res.created_panel_ids],
            detail=res.detail,
        )

    except svc.UserInDebtError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except svc.PanelLimitExceeded as e:
        raise HTTPException(status_code=409, detail=str(e))
    except svc.IdempotencyRequiredError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except svc.PanelPurchaseError as e:
        logger.warning("panels.purchase business error: %s", e)
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("panels.purchase failed: %s", e)
        raise HTTPException(status_code=500, detail="Временная ошибка. Повторите попытку позже.")

# =============================================================================
# Пояснения «для чайника»:
#   • Почему требуем Idempotency-Key? Любая денежная операция должна быть идемпотентной.
#     Если у клиента случится повтор (сетевой сбой/ретрай), банк не создаст дублей и вернёт
#     тот же результат (паттерн read-through, ключ — ваш Idempotency-Key).
#   • Зачем force_sync в GET? При открытии экрана Panels фронт может передать force_sync=1,
#     чтобы сервер перед формированием ответа выполнил «догон» энергии пользователя. Это
#     уменьшает расхождение «анимации» на фронте и реального состояния.
#   • Почему курсорная пагинация? OFFSET становится дорогим при больших объёмах. Курсоры
#     стабильны и быстры, фронт хранит next_cursor и подгружает «дальше».
#   • Почему нет ручек «суточной» генерации? Канон: источники истины — только посекундные
#     ставки GEN_PER_SEC_BASE_KWH/GEN_PER_SEC_VIP_KWH. Суточные значения — производные и
#     нигде в API не используются.
# =============================================================================
