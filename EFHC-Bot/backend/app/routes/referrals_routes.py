# -*- coding: utf-8 -*-
# backend/app/routes/referrals_routes.py
# =============================================================================
# Назначение кода:
#   • Публичные ручки «Рефералы»: витрины «Активные» и «Неактивные» с курсорной
#     пагинацией и ETag; счётчики для бейджей вкладок.
#
# Канон/инварианты:
#   • Активный реферал — тот, кто купил хотя бы одну панель (перманентный статус).
#   • Денежных действий здесь нет: только чтение витрин и мягкая синхронизация.
#   • Все числа Decimal сериализуются в строки с 8 знаками (канон).
#
# ИИ-защиты:
#   • Курсорная пагинация без OFFSET (ORDER BY created_at, child_user_id).
#   • ETag/If-None-Match для снижения нагрузки и стабильности клиента.
#   • «Принудительная синхронизация» при открытии: ensure_consistency() выполняется
#     по запросу (sync=true) — без падений, best-effort.
#
# Запреты:
#   • Нет P2P, нет модификаций балансов, нет обратной конверсии.
#   • Нет «ручной правки» активности: источник истины — panels (EXISTS).
# =============================================================================

from __future__ import annotations

from datetime import timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from pydantic import BaseModel, Field, conint
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.logging_core import get_logger
from backend.app.deps import get_db, d8  # централизованное Decimal(8) округление
from backend.app.services.referral_service import (
    get_counters,
    list_active_referrals,
    list_inactive_referrals,
    ensure_consistency,
    ReferralItemDTO,
)

logger = get_logger(__name__)
router = APIRouter(prefix="/referrals", tags=["referrals"])

# -----------------------------------------------------------------------------
# Локальный ETag-хелпер (стабильный SHA-256 по JSON)
# -----------------------------------------------------------------------------

def _build_etag(payload: Dict[str, Any]) -> str:
    import hashlib, json
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()

# -----------------------------------------------------------------------------
# Pydantic-модели ответов (Decimal → строки)
# -----------------------------------------------------------------------------

class ReferralItemOut(BaseModel):
    child_user_id: int
    telegram_id: Optional[int] = None
    username: Optional[str] = None
    joined_at: str = Field(..., description="ISO8601 UTC")
    is_active: bool
    total_generated_kwh: str = Field(..., description="Decimal(8) в строке")
    panels_count: int

class ReferralsPageOut(BaseModel):
    items: List[ReferralItemOut]
    next_cursor: Optional[str] = None

class ReferralCountersOut(BaseModel):
    total: int
    active: int
    inactive: int

# =============================================================================
# GET /referrals/counters — бейджи «Всего/Активные/Неактивные»
# =============================================================================

@router.get(
    "/counters",
    response_model=ReferralCountersOut,
    summary="Счётчики для вкладок: всего / активные / неактивные",
)
async def get_referral_counters(
    parent_user_id: conint(strict=True, ge=1),
    if_none_match: Optional[str] = Header(None, convert_underscores=True),
    db: AsyncSession = Depends(get_db),
) -> ReferralCountersOut:
    """
    Что делает:
      • Возвращает три счётчика для бейджей на вкладках.
      • Чистое чтение; денежной логики нет.
    Вход:
      • parent_user_id — владелец реферальной ссылки.
      • If-None-Match — поддержка 304 Not Modified.
    """
    dto = await get_counters(db, parent_user_id=int(parent_user_id))
    payload = {
        "scope": "referrals_counters",
        "parent_user_id": int(parent_user_id),
        "total": dto.total,
        "active": dto.active,
        "inactive": dto.inactive,
    }
    etag = _build_etag(payload)
    if if_none_match and if_none_match.strip() == etag:
        raise HTTPException(status_code=status.HTTP_304_NOT_MODIFIED, detail="Not Modified")

    resp = Response(
        content=ReferralCountersOut(total=dto.total, active=dto.active, inactive=dto.inactive).model_dump_json(),
        status_code=status.HTTP_200_OK,
        media_type="application/json",
        headers={"ETag": etag},
    )
    return resp  # type: ignore[return-value]

# =============================================================================
# GET /referrals/active — витрина «Активные» (курсоры, ETag, sync)
# =============================================================================

@router.get(
    "/active",
    response_model=ReferralsPageOut,
    summary="Витрина «Активные рефералы» с курсорной пагинацией",
)
async def get_referrals_active(
    parent_user_id: conint(strict=True, ge=1),
    limit: int = 100,
    cursor: Optional[str] = None,
    sync: bool = True,
    if_none_match: Optional[str] = Header(None, convert_underscores=True),
    db: AsyncSession = Depends(get_db),
) -> ReferralsPageOut:
    """
    Что делает:
      • Возвращает страницу «Активных» рефералов (купили ≥1 панель).
      • Курсорная пагинация без OFFSET по (created_at, child_user_id).
      • Если sync=true — мягкая «принудительная синхронизация» ensure_consistency()
        для конкретного parent_user_id (best-effort).
    """
    if sync:
        try:
            await ensure_consistency(db, parent_user_id=int(parent_user_id))
        except Exception as e:
            # не валим ручку; логируем и продолжаем
            logger.info("ensure_consistency(active) skipped: %s", e)

    page = await list_active_referrals(
        db,
        parent_user_id=int(parent_user_id),
        limit=int(limit),
        cursor=cursor,
    )

    payload = {
        "scope": "referrals_active",
        "parent_user_id": int(parent_user_id),
        "cursor": cursor or "",
        "items": [
            {
                "child_user_id": it.child_user_id,
                "telegram_id": it.telegram_id,
                "username": it.username or "",
                "joined_at": it.joined_at.astimezone(timezone.utc).isoformat(),
                "is_active": bool(it.is_active),
                "total_generated_kwh": str(d8(it.total_generated_kwh)),
                "panels_count": int(it.panels_count),
            }
            for it in page.items
        ],
        "next_cursor": page.next_cursor or "",
    }
    etag = _build_etag(payload)
    if if_none_match and if_none_match.strip() == etag:
        raise HTTPException(status_code=status.HTTP_304_NOT_MODIFIED, detail="Not Modified")

    resp = Response(
        content=ReferralsPageOut(
            items=[
                ReferralItemOut(
                    child_user_id=it.child_user_id,
                    telegram_id=it.telegram_id,
                    username=it.username,
                    joined_at=it.joined_at.astimezone(timezone.utc).isoformat(),
                    is_active=bool(it.is_active),
                    total_generated_kwh=str(d8(it.total_generated_kwh)),
                    panels_count=int(it.panels_count),
                )
                for it in page.items
            ],
            next_cursor=page.next_cursor,
        ).model_dump_json(),
        status_code=status.HTTP_200_OK,
        media_type="application/json",
        headers={"ETag": etag},
    )
    return resp  # type: ignore[return-value]

# =============================================================================
# GET /referrals/inactive — витрина «Неактивные» (курсоры, ETag, sync)
# =============================================================================

@router.get(
    "/inactive",
    response_model=ReferralsPageOut,
    summary="Витрина «Неактивные рефералы» с курсорной пагинацией",
)
async def get_referrals_inactive(
    parent_user_id: conint(strict=True, ge=1),
    limit: int = 100,
    cursor: Optional[str] = None,
    sync: bool = True,
    if_none_match: Optional[str] = Header(None, convert_underscores=True),
    db: AsyncSession = Depends(get_db),
) -> ReferralsPageOut:
    """
    Что делает:
      • Возвращает страницу «Неактивных» рефералов (ещё не купили панель).
      • Курсорная пагинация без OFFSET по (created_at, child_user_id).
      • Если sync=true — мягкая «принудительная синхронизация» ensure_consistency()
        для конкретного parent_user_id (best-effort).
    """
    if sync:
        try:
            await ensure_consistency(db, parent_user_id=int(parent_user_id))
        except Exception as e:
            logger.info("ensure_consistency(inactive) skipped: %s", e)

    page = await list_inactive_referrals(
        db,
        parent_user_id=int(parent_user_id),
        limit=int(limit),
        cursor=cursor,
    )

    payload = {
        "scope": "referrals_inactive",
        "parent_user_id": int(parent_user_id),
        "cursor": cursor or "",
        "items": [
            {
                "child_user_id": it.child_user_id,
                "telegram_id": it.telegram_id,
                "username": it.username or "",
                "joined_at": it.joined_at.astimezone(timezone.utc).isoformat(),
                "is_active": bool(it.is_active),
                "total_generated_kwh": str(d8(it.total_generated_kwh)),
                "panels_count": int(it.panels_count),
            }
            for it in page.items
        ],
        "next_cursor": page.next_cursor or "",
    }
    etag = _build_etag(payload)
    if if_none_match and if_none_match.strip() == etag:
        raise HTTPException(status_code=status.HTTP_304_NOT_MODIFIED, detail="Not Modified")

    resp = Response(
        content=ReferralsPageOut(
            items=[
                ReferralItemOut(
                    child_user_id=it.child_user_id,
                    telegram_id=it.telegram_id,
                    username=it.username,
                    joined_at=it.joined_at.astimezone(timezone.utc).isoformat(),
                    is_active=bool(it.is_active),
                    total_generated_kwh=str(d8(it.total_generated_kwh)),
                    panels_count=int(it.panels_count),
                )
                for it in page.items
            ],
            next_cursor=page.next_cursor,
        ).model_dump_json(),
        status_code=status.HTTP_200_OK,
        media_type="application/json",
        headers={"ETag": etag},
    )
    return resp  # type: ignore[return-value]

# =============================================================================
# Пояснения «для чайника»:
#  • Почему два списка? Это ускоряет работу UI и упрощает начисления за активных.
#  • Зачем курсор по (created_at, child_user_id)? Он устойчив к вставкам и не страдает
#    от смещений, как OFFSET; next_cursor кодирует последнюю пару значений.
#  • Что делает sync=true? Перед отдачей страницы выполняется мягкая проверка/ремонт
#    статуса активности (если в схеме есть referrals.is_active) — без падений, best-effort.
#  • Зачем ETag? Чтобы клиент мог быстро понять, что данные не изменились (304),
#    и не перекачивать одинаковые страницы.
# =============================================================================
