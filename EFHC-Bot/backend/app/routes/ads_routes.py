# -*- coding: utf-8 -*-
# backend/app/routes/ads_routes.py
# =============================================================================
# EFHC Bot — Роуты рекламы (rewarded ads)
# -----------------------------------------------------------------------------
# Что делает модуль:
#   • GET  /ads/slot              — выдаёт слот рекламы для пользователя:
#       - provider=builtin_timer  → выдаёт подписанный токен для фронта (таймер-провайдер).
#       - provider=adsgram        → отдаёт конфиг для мини-аппа Adsgram (без токенов).
#     Выбор провайдера можно задать ?provider=..., иначе берём первый включённый.
#
#   • POST /ads/callback/{provider} — приём колбэков провайдера:
#       - verify через integrations/ads_providers.py
#       - начисление бонуса ИДЕМПОТЕНТНО через tasks_service.confirm_ad_view_and_credit(...)
#
# Канон/инварианты:
#   • Денежные операции — только через банковские сервисы из tasks_service (bonus_balance).
#   • Идемпотентность обеспечивается provider_ref → idempotency_key.
#   • Никаких суточных ставок; суммы только Decimal с 8 знаками.
#   • P2P нет; только «пользователь ↔ банк».
#
# ИИ-защита:
#   • Два механизма определения пользователя:
#       1) Telegram WebApp initData (заголовок X-Telegram-Init-Data).
#       2) Запасной user_id (query/body) — если WebApp-подпись недоступна.
#   • ETag/If-None-Match для экономии трафика фронта.
#   • Мягкие 5xx — провайдер может безопасно ретраить тот же event/idempotency_key.
# =============================================================================

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from decimal import Decimal, ROUND_DOWN
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config_core import get_settings
from backend.app.core.logging_core import get_logger
from backend.app.deps import get_db, d8, etag  # централизованные хелперы
from backend.app.integrations.telegram_bot_api import TelegramAPI
from backend.app.integrations.ads_providers import (
    get_verified_event,
    issue_builtin_timer_token,
)
from backend.app.services.tasks_service import confirm_ad_view_and_credit

logger = get_logger(__name__)
settings = get_settings()
SCHEMA = getattr(settings, "DB_SCHEMA_CORE", "efhc_core")

# ----------------------------------------------------------------------------- 
# Конфиги провайдеров (из .env)
# -----------------------------------------------------------------------------

ENABLED = [x.strip() for x in str(getattr(settings, "ADS_ENABLED_PROVIDERS", "adsgram,builtin_timer")).split(",") if x.strip()]
DEFAULT_PROVIDER = ENABLED[0] if ENABLED else "builtin_timer"
BUILTIN_TTL_SEC = int(getattr(settings, "ADS_BUILTIN_TOKEN_TTL_SEC", 600) or 600)
BUILTIN_MIN_VIEW_SEC = int(getattr(settings, "ADS_BUILTIN_MIN_VIEW_SEC", 20) or 20)
ADSGRAM_PLACEMENT_ID = getattr(settings, "ADSGRAM_PLACEMENT_ID", None)

Q8 = Decimal("0.00000001")

# -----------------------------------------------------------------------------
# Pydantic-модели ответов
# -----------------------------------------------------------------------------

class AdSlotOut(BaseModel):
    provider: str = Field(..., description="adsgram | builtin_timer")
    task_code: str
    user_id: int
    reward_bonus_efhc: str
    # builtin_timer
    token: Optional[str] = Field(None, description="Подписанный токен (только для builtin_timer)")
    expires_at: Optional[str] = Field(None, description="ISO-время истечения токена (builtin_timer)")
    min_view_sec: Optional[int] = Field(None, description="Минимальное время просмотра (builtin_timer)")
    # adsgram
    placement_id: Optional[str] = Field(None, description="ID плейсмента Adsgram, если задан")

class AdCallbackResultOut(BaseModel):
    ok: bool
    provider: str
    task_code: str
    provider_ref: str
    reward_bonus_efhc: str
    replayed: bool = False

# -----------------------------------------------------------------------------
# Вспомогательно: извлекаем user_id (WebApp initData → fallback user_id)
# -----------------------------------------------------------------------------

async def _extract_user_id(request: Request, fallback_user_id: Optional[int]) -> int:
    """
    1) Если есть X-Telegram-Init-Data — проверяем подпись и берём real user.id.
    2) Иначе, если передан fallback_user_id — используем его.
    3) Иначе — 400.
    """
    init_header = (
        request.headers.get("X-Telegram-Init-Data")
        or request.headers.get("X-Tg-Init-Data")
        or request.headers.get("X-Init-Data")
    )
    if init_header:
        try:
            tg = TelegramAPI()
            v = tg.verify_webapp_init_data(init_header)
            if v.ok and v.user_id:
                return int(v.user_id)
        except Exception as e:
            # падать не будем — попробуем fallback
            logger.info("initData verify failed, fallback to user_id: %s", e)

    if fallback_user_id and int(fallback_user_id) > 0:
        return int(fallback_user_id)

    raise HTTPException(status_code=400, detail="Не удалось определить пользователя (нет initData и user_id).")

# -----------------------------------------------------------------------------
# GET /ads/slot — выдача слота рекламы
# -----------------------------------------------------------------------------

router = APIRouter(prefix="/ads", tags=["ads"])

@router.get("/slot", response_model=AdSlotOut, summary="Выдать слот рекламы (token/config) для пользователя")
async def get_ad_slot(
    request: Request,
    response: Response,
    task_code: str,
    provider: Optional[str] = None,
    user_id: Optional[int] = None,  # fallback, когда нет initData
    db: AsyncSession = Depends(get_db),
):
    """
    Возвращает информацию для показа рекламы:
      • builtin_timer → подписанный токен (проверка по колбэку).
      • adsgram       → placement_id/конфиг (начисление по внешнему колбэку).
    """
    uid = await _extract_user_id(request, user_id)
    prov = (provider or DEFAULT_PROVIDER).strip().lower()
    if prov not in ENABLED:
        raise HTTPException(status_code=400, detail=f"Провайдер '{prov}' не активен")

    # 1) найдём задание вида type='watch_ad', is_active=TRUE
    row = (await db.execute(
        text(
            f"""
            SELECT task_code, reward_bonus_efhc, is_active, type, meta
              FROM {SCHEMA}.tasks
             WHERE task_code = :code
             LIMIT 1
            """
        ),
        {"code": task_code},
    )).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Задание не найдено")
    if not bool(row[2]):
        raise HTTPException(status_code=400, detail="Задание отключено")
    if str(row[3] or "") != "watch_ad":
        raise HTTPException(status_code=400, detail="Неверный тип задания (ожидается watch_ad)")

    reward = Decimal(str(row[1])).quantize(Q8, rounding=ROUND_DOWN)

    if prov == "builtin_timer":
        token = issue_builtin_timer_token(
            user_id=uid,
            task_code=task_code,
            reward_bonus_efhc=reward,
            ttl_sec=BUILTIN_TTL_SEC,
        )
        exp_iso = (datetime.now(tz=timezone.utc) + timedelta(seconds=BUILTIN_TTL_SEC)).isoformat()
        # ETag на базовые поля — чтобы фронт мог кэшировать
        et = etag(f"slot:{uid}:{task_code}:{prov}:{reward}:{exp_iso}")
        if request.headers.get("If-None-Match") == et:
            response.status_code = status.HTTP_304_NOT_MODIFIED
            response.headers["ETag"] = et
            return
        response.headers["ETag"] = et
        return AdSlotOut(
            provider="builtin_timer",
            task_code=task_code,
            user_id=uid,
            reward_bonus_efhc=str(d8(reward)),
            token=token,
            expires_at=exp_iso,
            min_view_sec=BUILTIN_MIN_VIEW_SEC,
        )

    # prov == "adsgram"
    placement_id = ADSGRAM_PLACEMENT_ID
    et = etag(f"slot:{uid}:{task_code}:{prov}:{reward}:{placement_id or ''}")
    if request.headers.get("If-None-Match") == et:
        response.status_code = status.HTTP_304_NOT_MODIFIED
        response.headers["ETag"] = et
        return
    response.headers["ETag"] = et
    return AdSlotOut(
        provider="adsgram",
        task_code=task_code,
        user_id=uid,
        reward_bonus_efhc=str(d8(reward)),
        placement_id=(placement_id or None),
    )

# -----------------------------------------------------------------------------
# POST /ads/callback/{provider} — приём колбэков провайдера
# -----------------------------------------------------------------------------

@router.post("/callback/{provider}", response_model=AdCallbackResultOut, summary="Колбэк провайдера рекламы (идемпотентное начисление)")
async def ads_callback(
    provider: str,
    request: Request,
    response: Response,
    body: Dict[str, Any],
    db: AsyncSession = Depends(get_db),
    idempotency_key_hdr: Optional[str] = Header(default=None, convert_underscores=False, alias="Idempotency-Key"),
):
    """
    Принимает событие показа от провайдера:
      1) verify → VerifiedAdEvent (user_id, task_code, reward, idempotency_key).
      2) начисляет бонусы через tasks_service.confirm_ad_view_and_credit(...) ИДЕМПОТЕНТНО.
    ВАЖНО:
      • По Канону «все денежные POST требуют Idempotency-Key». У внешних провайдеров его
        может не быть → мы опираемся на стабильный idempotency_key из VerifiedAdEvent
        (обычно tx_id). Если заголовок всё-таки передан — добавим его к ключу (усиление).
    """
    client_ip = request.client.host if request.client else None

    # 1) Верификация события у провайдера
    try:
        evt = await get_verified_event(provider, payload=body or {}, headers=dict(request.headers), client_ip=client_ip)
    except ValueError as e:
        # Ошибки верификации → 400/403
        reason = str(e)
        if reason in ("provider_disabled", "ip_not_allowed", "missing_token", "token_expired", "signature_mismatch",
                      "token_signature_mismatch", "missing_signature", "hash_mismatch", "invalid_user_id"):
            raise HTTPException(status_code=403, detail=reason)
        raise HTTPException(status_code=400, detail=reason)
    except Exception as e:
        logger.error("ads.callback verify failed prov=%s err=%s", provider, e)
        raise HTTPException(status_code=500, detail="internal_error")

    # 2) Усиленная идемпотентность: склеим ключ с заголовком, если он есть
    idk = evt.idempotency_key
    if idempotency_key_hdr and idempotency_key_hdr.strip():
        idk = f"{evt.idempotency_key}|hdr:{idempotency_key_hdr.strip()}"

    # 3) Начисление бонусов (идемпотентно)
    try:
        result = await confirm_ad_view_and_credit(
            db,
            user_id=int(evt.user_id),
            task_code=str(evt.task_code),
            provider=str(evt.provider),
            provider_ref=str(evt.provider_ref),
            reward_bonus_efhc=evt.reward_bonus_efhc,
            idempotency_key=idk,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("ads.callback credit failed prov=%s uid=%s task=%s err=%s",
                     provider, evt.user_id, evt.task_code, e)
        raise HTTPException(status_code=500, detail="credit_failed")

    # 4) Ответ + ETag
    et = etag(f"ads_cb:{evt.provider}:{evt.provider_ref}:{evt.user_id}:{evt.task_code}:{result.get('replayed', False)}")
    response.headers["ETag"] = et

    return AdCallbackResultOut(
        ok=True,
        provider=str(evt.provider),
        task_code=str(evt.task_code),
        provider_ref=str(evt.provider_ref),
        reward_bonus_efhc=str(d8(result.get("reward_bonus_efhc", evt.reward_bonus_efhc))),
        replayed=bool(result.get("replayed", False)),
    )
