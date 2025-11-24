# -*- coding: utf-8 -*-
# backend/app/routes/exchange_routes.py
# =============================================================================
# EFHC Bot — Роуты обменника kWh → EFHC (1:1, только в одну сторону)
# -----------------------------------------------------------------------------
# Канон:
#   • Обмен возможен ТОЛЬКО available_kwh → EFHC по фиксированному курсу 1:1.
#   • Никаких обратных обменов EFHC → kWh.
#   • Все денежные POST — строго с Idempotency-Key (или client_nonce) через deps.
#   • Списки — только cursor-based пагинация (keyset).
#   • Ставки генерации per-second живут в сервисах; роуты НИЧЕГО не считают.
#
# ИИ-защита:
#   • Дружелюбные ошибки, валидация входа, мягкая деградация.
#   • ETag для предпросмотра (экономия трафика).
#   • Ключи идемпотентности нормализуются и логируются через сервисы.
# =============================================================================

from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Any, Dict, Optional, Tuple

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config_core import get_settings
from backend.app.core.logging_core import get_logger
from backend.app.deps import get_db, require_idempotency_key  # <— единый валидатор канона

from backend.app.services.exchange_service import (
    preview_exchange as svc_preview_exchange,
    exchange_kwh_to_efhc as svc_exchange_kwh_to_efhc,
)
from backend.app.services.energy_service import generate_for_user as svc_generate_for_user

logger = get_logger(__name__)
settings = get_settings()
SCHEMA = getattr(settings, "DB_SCHEMA_CORE", "efhc_core")

# -----------------------------------------------------------------------------
# Утилиты (точность, ETag, курсор)
# -----------------------------------------------------------------------------

EFHC_DECIMALS: int = int(getattr(settings, "EFHC_DECIMALS", 8) or 8)
Q8 = Decimal(1).scaleb(-EFHC_DECIMALS)

def d8(x: Any) -> Decimal:
    return Decimal(str(x)).quantize(Q8, rounding=ROUND_DOWN)

def _etag(payload: Dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def _encode_cursor(ts: datetime, row_id: int) -> str:
    blob = f"{int((ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)).timestamp())}|{row_id}".encode("utf-8")
    return base64.urlsafe_b64encode(blob).decode("ascii")

def _decode_cursor(cur: str) -> Tuple[int, int]:
    try:
        raw = base64.urlsafe_b64decode(cur.encode("ascii")).decode("utf-8")
        ts_unix_str, row_id_str = raw.split("|", 1)
        return int(ts_unix_str), int(row_id_str)
    except Exception:
        raise HTTPException(status_code=400, detail="Некорректный cursor")

# -----------------------------------------------------------------------------
# Pydantic-схемы
# -----------------------------------------------------------------------------

class ExchangePreviewOut(BaseModel):
    ok: bool
    available_kwh: str
    max_exchangeable_kwh: str
    rate_kwh_to_efhc: str
    detail: str

class ExchangeConvertIn(BaseModel):
    # Можно передать "всё доступное", оставив пустым; тогда сервис обменяет max.
    amount_kwh: Optional[str] = Field(None, description="Сколько kWh обменять (строкой с 8 знаками). Если пропустить — обменять максимум.")
    client_nonce: Optional[str] = Field(None, description="Альтернатива заголовку Idempotency-Key (строка для идемпотентности)")

    @field_validator("amount_kwh")
    @classmethod
    def _valid_amount(cls, v: Optional[str]) -> Optional[str]:
        if v is None or not str(v).strip():
            return None
        try:
            _ = Decimal(str(v))
        except Exception:
            raise ValueError("amount_kwh должно быть числом")
        return str(v)

class ExchangeConvertOut(BaseModel):
    ok: bool
    exchanged_kwh: str
    credited_efhc: str
    new_available_kwh: str
    new_main_balance: str
    log_id: Optional[int] = None
    detail: str

class ExchangeHistoryItem(BaseModel):
    id: int
    created_at: str
    amount_kwh: str
    amount_efhc: str
    reason: str

class ExchangeHistoryOut(BaseModel):
    items: list[ExchangeHistoryItem]
    next_cursor: Optional[str] = None

# -----------------------------------------------------------------------------
# Router
# -----------------------------------------------------------------------------

router = APIRouter(prefix="/exchange", tags=["exchange"])

# -----------------------------------------------------------------------------
# Предпросмотр обмена (без списаний), с ETag и опцией fresh-синхронизации
# -----------------------------------------------------------------------------

@router.get("/preview/{user_id}", response_model=ExchangePreviewOut, summary="Предпросмотр обмена kWh→EFHC (без списаний)")
async def preview(
    user_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    fresh: bool = Query(True, description="Идемпотентно догнать генерацию перед предпросмотром"),
) -> Response:
    # Опционально догоним генерацию, чтобы цифры были «как на сервере»
    if fresh:
        try:
            await svc_generate_for_user(db, user_id=user_id)
        except Exception as e:
            logger.warning("preview: fresh generate failed uid=%s: %s", user_id, e)

    try:
        p = await svc_preview_exchange(db, user_id=user_id)
    except Exception as e:
        logger.error("preview: service error uid=%s: %s", user_id, e)
        raise HTTPException(status_code=500, detail="Временная ошибка предпросмотра")

    payload = ExchangePreviewOut(
        ok=bool(p.ok),
        available_kwh=str(d8(p.available_kwh)),
        max_exchangeable_kwh=str(d8(p.max_exchangeable_kwh)),
        rate_kwh_to_efhc=str(d8(p.rate_kwh_to_efhc)),
        detail=p.detail,
    ).model_dump()

    et = _etag(payload)
    inm = request.headers.get("if-none-match")
    if inm and inm == et:
        return Response(status_code=304, headers={"ETag": et})
    return JSONResponse(content=payload, headers={"ETag": et})

# -----------------------------------------------------------------------------
# Обмен kWh → EFHC (денежный POST) — строго с идемпотентностью (deps)
# -----------------------------------------------------------------------------

@router.post("/convert/{user_id}", response_model=ExchangeConvertOut, summary="Обменять kWh→EFHC (1:1). Idempotency-Key обязателен")
async def convert(
    user_id: int,
    body: ExchangeConvertIn = Body(...),
    db: AsyncSession = Depends(get_db),
    idk_digest: str = Depends(require_idempotency_key),  # <— единый жёсткий валидатор
) -> ExchangeConvertOut:
    """
    Требование канона:
      • Должен быть либо заголовок Idempotency-Key, либо client_nonce (query).
      • deps.require_idempotency_key возвращает нормализованный sha256-ключ (hex).
    Если нужен режим «только заголовок», можем переключить зависимость на
    вариант, проверяющий ТОЛЬКО Idempotency-Key (без client_nonce).
    """
    amount_kwh: Optional[Decimal] = d8(body.amount_kwh) if body.amount_kwh else None

    try:
        # Сервис сам проверит лимиты, доступность, 1:1 и создаст зеркальные логи банка.
        res = await svc_exchange_kwh_to_efhc(
            db,
            user_id=user_id,
            amount_kwh=amount_kwh,        # None → «обменять максимум»
            idempotency_key=f"exchange:{user_id}:{idk_digest}",
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("convert: service error uid=%s: %s", user_id, e)
        raise HTTPException(status_code=500, detail="Ошибка обмена, попробуйте позже")

    return ExchangeConvertOut(
        ok=bool(res.ok),
        exchanged_kwh=str(d8(res.exchanged_kwh)),
        credited_efhc=str(d8(res.credited_efhc)),
        new_available_kwh=str(d8(res.new_available_kwh)),
        new_main_balance=str(d8(res.new_main_balance)),
        log_id=getattr(res, "log_id", None),
        detail=res.detail,
    )

# -----------------------------------------------------------------------------
# История обменов (cursor-based, без «offset»; только операции обмена)
# -----------------------------------------------------------------------------

@router.get("/history/{user_id}", response_model=ExchangeHistoryOut, summary="История обменов пользователя (keyset-pagination)")
async def history(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    cursor: Optional[str] = Query(None, description="Курсор продолжения (получен в предыдущем ответе)"),
    limit: int = Query(50, ge=1, le=200, description="Сколько записей вернуть (1..200)"),
) -> ExchangeHistoryOut:
    """
    Историю строим по журналу efhc_transfers_log с reason='exchange_kwh_to_efhc'.
    Берём только кредиты на пользователя (движение «банк → пользователь»).
    Keyset: сортировка по (created_at desc, id desc); курсор — b64(ts|id).
    """
    ts_lt: Optional[int] = None
    id_lt: Optional[int] = None
    if cursor:
        ts_lt, id_lt = _decode_cursor(cursor)

    params: Dict[str, Any] = {"uid": int(user_id), "lim": int(limit) + 1}
    cond = ""
    if ts_lt is not None and id_lt is not None:
        cond = "AND (l.created_at < to_timestamp(:ts) OR (l.created_at = to_timestamp(:ts) AND l.id < :lid))"
        params.update({"ts": ts_lt, "lid": id_lt})

    row = await db.execute(
        text(
            f"""
            SELECT l.id,
                   l.created_at,
                   l.amount,            -- EFHC == kWh (1:1)
                   l.reason
              FROM {SCHEMA}.efhc_transfers_log l
             WHERE l.user_id = :uid
               AND l.direction = 'credit'
               AND l.reason = 'exchange_kwh_to_efhc'
               {cond}
             ORDER BY l.created_at DESC, l.id DESC
             LIMIT :lim
            """
        ),
        params,
    )

    fetched = row.fetchall()
    has_more = len(fetched) > limit
    items = fetched[:limit]

    out_items = []
    next_cur = None
    for rec in items:
        rid = int(rec[0])
        rts = rec[1]  # datetime
        amt = d8(rec[2])
        reason = str(rec[3])

        out_items.append(ExchangeHistoryItem(
            id=rid,
            created_at=rts.replace(tzinfo=timezone.utc).isoformat() if rts.tzinfo is None else rts.isoformat(),
            amount_kwh=str(amt),
            amount_efhc=str(amt),  # 1:1
            reason=reason,
        ))

    if has_more:
        last = items[-1]
        last_id = int(last[0])
        last_ts: datetime = last[1]
        next_cur = _encode_cursor(last_ts if last_ts.tzinfo else last_ts.replace(tzinfo=timezone.utc), last_id)

    return ExchangeHistoryOut(items=out_items, next_cursor=next_cur)

# =============================================================================
# Пояснения «для чайника»:
#   • /preview — только чтение, может догонять энергию (fresh=1) и отдаёт ETag.
#   • /convert — денежный POST; строго требуем Idempotency-Key/client_nonce через deps.
#   • /history — keyset-пагинация по журналу обмена; курсор b64(ts|id).
#   • Роуты НЕ считают ставки генерации сами — всё в сервисах.
# =============================================================================
