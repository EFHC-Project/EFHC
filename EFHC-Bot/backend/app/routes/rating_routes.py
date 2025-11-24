# -*- coding: utf-8 -*-
# backend/app/routes/rating_routes.py
# =============================================================================
# Назначение кода:
#   • Публичные ручки рейтинга: витрина «Я + TOP» и постраничный TOP.
#   • «Принудительная синхронизация» при открытии: ensure_latest_snapshot()
#     освежает снапшот рейтинга конкурентно-безопасно (advisory-lock).
#
# Канон/инварианты:
#   • Источник истины — total_generated_kwh, никакой «суточной» логики.
#   • «Я» отображается с реальным местом; в TOP «Я» не дублируется.
#   • Все числа — Decimal(8), строки в JSON (канон сериализации).
#
# ИИ-защиты:
#   • ensure_latest_snapshot(): попытка освежить снапшот; при сбоях — деградация
#     к последнему доступному без падения ручек.
#   • ETag/If-None-Match: экономия трафика, стабильность клиента.
#   • Курсорная пагинация по rank_pos ASC без OFFSET — устойчива на больших данных.
#
# Запреты:
#   • Нет денежных операций, нет P2P, нет модификации балансов/тоталов.
#   • Нет «живого» перерасчёта позиций вне снапшота (только snapshot).
# =============================================================================

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from pydantic import BaseModel, Field, conint
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.logging_core import get_logger
from backend.app.deps import get_db, d8  # централизованное Decimal(8)

# Витринный сервис рейтинга: снапшоты и выдача «Я + TOP»
from backend.app.services.ranks_service import (
    ensure_latest_snapshot,
    get_my_plus_top,
    list_top_page,
    RankItemDTO,
)

logger = get_logger(__name__)
router = APIRouter(prefix="/rating", tags=["rating"])

# =============================================================================
# Локальные хелперы ETag (стабильное хеширование полезной нагрузки)
# =============================================================================

def _build_etag(payload: Dict[str, Any]) -> str:
    import hashlib, json
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()

# =============================================================================
# Pydantic-модели ответа (строки для Decimal во избежание потери точности)
# =============================================================================

class RankItemOut(BaseModel):
    user_id: int
    telegram_id: Optional[int] = None
    username: Optional[str] = None
    total_generated_kwh: str = Field(..., description="Тотал kWh (строка Decimal(8))")
    rank_pos: int
    is_vip: bool

class MyPlusTopOut(BaseModel):
    snapshot_at: str
    me: RankItemOut
    top: List[RankItemOut]
    next_cursor: Optional[str]

class TopPageOut(BaseModel):
    snapshot_at: str
    items: List[RankItemOut]
    next_cursor: Optional[str]

# =============================================================================
# GET /rating/my-plus-top — «Я + TOP» с курсором и ETag
# =============================================================================

@router.get(
    "/my-plus-top",
    response_model=MyPlusTopOut,
    summary="Витрина рейтинга: «Я + TOP» (без дублирования «Я»)",
)
async def get_rating_my_plus_top(
    user_id: conint(strict=True, ge=1),
    limit: int = 100,
    cursor: Optional[str] = None,
    force_refresh: bool = False,
    max_age_minutes: int = 10,
    if_none_match: Optional[str] = Header(None, convert_underscores=True),
    db: AsyncSession = Depends(get_db),
) -> MyPlusTopOut:
    """
    Что делает:
      • Обеспечивает актуальный снапшот (ensure_latest_snapshot).
      • Возвращает «Я» (реальное место) + TOP-страницу без «Я».
      • Курсорная пагинация по rank_pos ASC (без OFFSET).
      • Поддерживает ETag/If-None-Match для экономии трафика.
    Вход:
      • user_id — внутренний ID пользователя.
      • limit (<=200), cursor — курсор по rank_pos, force_refresh — принудить rebuild.
    Выход:
      • snapshot_at (ISO UTC), me, top[], next_cursor.
    Исключения:
      • 500 — при непредвиденных сбоях базы (деградация в сервисе снижает вероятность).
    """
    # 1) обеспечить свежий снапшот с ИИ-защитой (advisory-lock, деградация)
    snapshot_at = await ensure_latest_snapshot(
        db,
        max_age_minutes=int(max_age_minutes),
        force_refresh=bool(force_refresh),
    )

    # 2) собрать витрину «Я + TOP»
    dto = await get_my_plus_top(
        db,
        user_id=int(user_id),
        limit=int(limit),
        cursor=cursor,
        force_refresh=False,          # уже освежали выше
        max_age_minutes=int(max_age_minutes),
    )

    # 3) собрать полезную нагрузку для ETag
    payload = {
        "scope": "rating_my_plus_top",
        "snapshot_at": dto.snapshot_at.astimezone(timezone.utc).isoformat(),
        "me": {
            "user_id": dto.me.user_id,
            "telegram_id": dto.me.telegram_id,
            "username": dto.me.username or "",
            "total_generated_kwh": str(d8(dto.me.total_generated_kwh)),
            "rank_pos": dto.me.rank_pos,
            "is_vip": bool(dto.me.is_vip),
        },
        "top": [
            {
                "user_id": it.user_id,
                "telegram_id": it.telegram_id,
                "username": it.username or "",
                "total_generated_kwh": str(d8(it.total_generated_kwh)),
                "rank_pos": it.rank_pos,
                "is_vip": bool(it.is_vip),
            }
            for it in dto.top
        ],
        "next_cursor": dto.next_cursor or "",
    }
    etag = _build_etag(payload)

    # 4) отдаём 304 Not Modified, если ETag совпал
    if if_none_match and if_none_match.strip() == etag:
        raise HTTPException(status_code=status.HTTP_304_NOT_MODIFIED, detail="Not Modified")

    # 5) формируем ответ (строки для Decimal)
    resp = Response(
        content=MyPlusTopOut(
            snapshot_at=dto.snapshot_at.astimezone(timezone.utc).isoformat(),
            me=RankItemOut(
                user_id=dto.me.user_id,
                telegram_id=dto.me.telegram_id,
                username=dto.me.username,
                total_generated_kwh=str(d8(dto.me.total_generated_kwh)),
                rank_pos=dto.me.rank_pos,
                is_vip=bool(dto.me.is_vip),
            ),
            top=[
                RankItemOut(
                    user_id=it.user_id,
                    telegram_id=it.telegram_id,
                    username=it.username,
                    total_generated_kwh=str(d8(it.total_generated_kwh)),
                    rank_pos=it.rank_pos,
                    is_vip=bool(it.is_vip),
                )
                for it in dto.top
            ],
            next_cursor=dto.next_cursor,
        ).model_dump_json(),
        status_code=status.HTTP_200_OK,
        media_type="application/json",
        headers={"ETag": etag},
    )
    return resp  # type: ignore[return-value]

# =============================================================================
# GET /rating/top — постраничный TOP (без «Я»), для пролистывания
# =============================================================================

@router.get(
    "/top",
    response_model=TopPageOut,
    summary="TOP-страница рейтинга (листинг по rank_pos ASC, без «Я»)",
)
async def get_rating_top(
    limit: int = 100,
    cursor: Optional[str] = None,
    exclude_user_id: Optional[int] = None,
    force_refresh: bool = False,
    max_age_minutes: int = 10,
    if_none_match: Optional[str] = Header(None, convert_underscores=True),
    db: AsyncSession = Depends(get_db),
) -> TopPageOut:
    """
    Что делает:
      • Возвращает страницу TOP по снапшоту (ORDER BY rank_pos ASC) с курсором.
      • exclude_user_id — исключить «Я» из листинга (чтобы не дублировать).
      • ETag/If-None-Match — для экономии трафика при неизменившихся данных.
    Важно:
      • Денежных эффектов нет, это чистое чтение снапшота.
    """
    # 1) обеспечить свежий снапшот
    snapshot_at = await ensure_latest_snapshot(
        db,
        max_age_minutes=int(max_age_minutes),
        force_refresh=bool(force_refresh),
    )

    # 2) получить страницу TOP
    items_dto, next_cursor = await list_top_page(
        db,
        snapshot_at=snapshot_at,
        limit=int(limit),
        cursor=cursor,
        exclude_user_id=(int(exclude_user_id) if exclude_user_id else None),
    )

    payload = {
        "scope": "rating_top",
        "snapshot_at": snapshot_at.astimezone(timezone.utc).isoformat(),
        "items": [
            {
                "user_id": it.user_id,
                "telegram_id": it.telegram_id,
                "username": it.username or "",
                "total_generated_kwh": str(d8(it.total_generated_kwh)),
                "rank_pos": it.rank_pos,
                "is_vip": bool(it.is_vip),
            }
            for it in items_dto
        ],
        "next_cursor": next_cursor or "",
    }
    etag = _build_etag(payload)
    if if_none_match and if_none_match.strip() == etag:
        raise HTTPException(status_code=status.HTTP_304_NOT_MODIFIED, detail="Not Modified")

    resp = Response(
        content=TopPageOut(
            snapshot_at=snapshot_at.astimezone(timezone.utc).isoformat(),
            items=[
                RankItemOut(
                    user_id=it.user_id,
                    telegram_id=it.telegram_id,
                    username=it.username,
                    total_generated_kwh=str(d8(it.total_generated_kwh)),
                    rank_pos=it.rank_pos,
                    is_vip=bool(it.is_vip),
                )
                for it in items_dto
            ],
            next_cursor=next_cursor,
        ).model_dump_json(),
        status_code=status.HTTP_200_OK,
        media_type="application/json",
        headers={"ETag": etag},
    )
    return resp  # type: ignore[return-value]

# =============================================================================
# Пояснения «для чайника»:
#  • Зачем снапшоты? Рейтинг на больших объёмах считать «на лету» дорого. Снапшот
#    фиксирует срез (snapshot_at) и даёт быстрые, повторяемые ответы.
#  • Почему курсор по rank_pos? Курсор по rank_pos ASC стабилен и не страдает от
#    смещений, как OFFSET. Каждая следующая страница берёт rank_pos > last.
#  • Что делает force_refresh? Просит сервис попробовать пересобрать снапшот
#    прямо сейчас (адвайзори-лок защитит от гонок). При сбое — вернётся последний.
#  • Почему «Я» не дублируется в TOP? Чтобы не было повторов в выдаче; «Я» идёт
#    отдельным блоком, а TOP — без него.
# =============================================================================
