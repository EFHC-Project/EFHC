# -*- coding: utf-8 -*-
# backend/app/routes/user_routes.py
# =============================================================================
# EFHC Bot — Пользовательские ручки (профиль, балансы, подсказки для UI)
# -----------------------------------------------------------------------------
# Что делает модуль:
#   • Возвращает профиль пользователя и ключевые балансы в одном ответе
#     (main_balance, bonus_balance, available_kwh, total_generated_kwh).
#   • Добавляет готовые поля для фронтенда ТОЛЬКО в посекундном каноне:
#       gen_per_sec_base_kwh = 0.00000692
#       gen_per_sec_vip_kwh  = 0.00000741
#     Фронтенд не пересчитывает ставки — использует их как источник истины.
#   • Даёт безопасные подсказки для экранов: panels_summary, preview_exchange.
#
# ИИ-защита/надёжность:
#   • Все суммы — Decimal с 8 знаками (округление вниз).
#   • Дружелюбные сообщения об ошибках (например, если пользователь не найден).
#   • Никаких денежных операций и P2P-запрещённых действий — здесь только чтение.
#   • Любые расчёты, влияющие на баланс, выполняются исключительно в сервисах,
#     а не в роутере (канон «единый банк/единые сервисы»).
#   • Никаких «суточных» ставок и эндпоинтов — только посекундные константы.
# =============================================================================

from __future__ import annotations

from decimal import Decimal, ROUND_DOWN
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config_core import get_settings
from backend.app.core.logging_core import get_logger
from backend.app.deps import get_db, d8  # точное округление централизовано в deps
from backend.app.services.panels_service import panels_summary as svc_panels_summary
from backend.app.services.exchange_service import preview_exchange as svc_preview_exchange

logger = get_logger(__name__)
settings = get_settings()
SCHEMA = getattr(settings, "DB_SCHEMA_CORE", "efhc_core")

# -----------------------------------------------------------------------------
# Канонические посекундные ставки генерации (строго по канону)
# -----------------------------------------------------------------------------
# Только эти имена и значения используются в коде:
#   GEN_PER_SEC_BASE_KWH = 0.00000692  (ставка без VIP)
#   GEN_PER_SEC_VIP_KWH  = 0.00000741  (ставка VIP)
# Если переменные не заданы в окружении — берём канон по умолчанию.

GEN_PER_SEC_BASE_KWH = Decimal(
    str(getattr(settings, "GEN_PER_SEC_BASE_KWH", "0.00000692") or "0.00000692")
).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)

GEN_PER_SEC_VIP_KWH = Decimal(
    str(getattr(settings, "GEN_PER_SEC_VIP_KWH", "0.00000741") or "0.00000741")
).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)

# -----------------------------------------------------------------------------
# Pydantic-схемы ответов
# -----------------------------------------------------------------------------

class UserProfileOut(BaseModel):
    telegram_id: int = Field(..., description="Телеграм-ID пользователя")
    username: Optional[str] = Field(None, description="Никнейм в Telegram (если есть)")
    is_vip: bool = Field(..., description="Флаг VIP-статуса (определяется NFT)")
    main_balance: str = Field(..., description="Основной баланс EFHC (строкой с 8 знаками)")
    bonus_balance: str = Field(..., description="Бонусный баланс EFHC (строкой с 8 знаками)")
    available_kwh: str = Field(..., description="Доступная к обмену энергия (кВт·ч)")
    total_generated_kwh: str = Field(..., description="Общая сгенерированная энергия (для рейтинга)")
    # Посекундные ставки для фронтенда (канон, фронт не пересчитывает):
    gen_per_sec_base_kwh: str = Field(..., description="Ставка без VIP, кВт·ч/сек (канон)")
    gen_per_sec_vip_kwh: str = Field(..., description="Ставка VIP, кВт·ч/сек (канон)")

class PanelsSummaryOut(BaseModel):
    active_count: int
    total_generated_by_panels: str
    nearest_expire_at: Optional[str] = None  # ISO-дата или None

class ExchangePreviewOut(BaseModel):
    ok: bool
    available_kwh: str
    max_exchangeable_kwh: str
    rate_kwh_to_efhc: str
    detail: str

# -----------------------------------------------------------------------------
# Router
# -----------------------------------------------------------------------------

router = APIRouter(prefix="/user", tags=["user"])

# -----------------------------------------------------------------------------
# Получение профиля по Telegram ID (канонический контракт)
# -----------------------------------------------------------------------------

@router.get("/by-telegram/{telegram_id}", response_model=UserProfileOut, summary="Профиль и балансы пользователя (по Telegram ID)")
async def get_user_profile_by_telegram(telegram_id: int, db: AsyncSession = Depends(get_db)) -> UserProfileOut:
    """
    Возвращает профиль и балансы.
    Фронтенд получает ТОЛЬКО посекундные ставки (gen_per_sec_*), никаких daily.
    """
    row = await db.execute(
        text(
            f"""
            SELECT
              u.telegram_id,
              u.username,
              COALESCE(u.is_vip, FALSE) AS is_vip,
              COALESCE(u.main_balance, 0) AS main_balance,
              COALESCE(u.bonus_balance, 0) AS bonus_balance,
              COALESCE(u.available_kwh, 0) AS available_kwh,
              COALESCE(u.total_generated_kwh, 0) AS total_generated_kwh
            FROM {SCHEMA}.users u
            WHERE u.telegram_id = :tg
            LIMIT 1
            """
        ),
        {"tg": int(telegram_id)},
    )
    rec = row.fetchone()
    if not rec:
        # ИИ-дружелюбная ошибка: пользователю/клиенту понятно, что делать дальше
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Пользователь с telegram_id={telegram_id} не найден."
        )

    return UserProfileOut(
        telegram_id=int(rec[0]),
        username=rec[1],
        is_vip=bool(rec[2]),
        main_balance=str(d8(rec[3])),
        bonus_balance=str(d8(rec[4])),
        available_kwh=str(d8(rec[5])),
        total_generated_kwh=str(d8(rec[6])),
        gen_per_sec_base_kwh=str(GEN_PER_SEC_BASE_KWH),
        gen_per_sec_vip_kwh=str(GEN_PER_SEC_VIP_KWH),
    )

# -----------------------------------------------------------------------------
# Короткая сводка панелей (для экрана Panels)
# -----------------------------------------------------------------------------

@router.get("/{user_id}/panels-summary", response_model=PanelsSummaryOut, summary="Сводка по панелям пользователя")
async def get_panels_summary(user_id: int, db: AsyncSession = Depends(get_db)) -> PanelsSummaryOut:
    """
    Возвращает:
      • active_count — число активных панелей,
      • total_generated_by_panels — суммарная генерация по полю панели (для UI),
      • nearest_expire_at — ближайшая дата окончания срока у активной панели.
    """
    try:
        s = await svc_panels_summary(db, user_id=user_id)
    except Exception as e:
        logger.warning("panels_summary failed for user %s: %s", user_id, e)
        raise HTTPException(status_code=500, detail="Временная ошибка. Повторите попытку.")
    return PanelsSummaryOut(
        active_count=int(s["active_count"]),
        total_generated_by_panels=str(d8(s["total_generated_by_panels"])),
        nearest_expire_at=(s["nearest_expire_at"].isoformat() if s.get("nearest_expire_at") else None),
    )

# -----------------------------------------------------------------------------
# Безопасный предпросмотр обмена энергии → EFHC (ничего не списывает)
# -----------------------------------------------------------------------------

@router.get("/{user_id}/exchange/preview", response_model=ExchangePreviewOut, summary="Предпросмотр обмена kWh→EFHC (без списаний)")
async def preview_exchange(user_id: int, db: AsyncSession = Depends(get_db)) -> ExchangePreviewOut:
    """
    Показывает, сколько доступно к обмену прямо сейчас.
    НЕ выполняет списаний и начислений — только подсказка для UI.
    """
    try:
        p = await svc_preview_exchange(db, user_id=user_id)
    except Exception as e:
        logger.warning("exchange_preview failed for user %s: %s", user_id, e)
        raise HTTPException(status_code=500, detail="Временная ошибка. Повторите попытку.")

    return ExchangePreviewOut(
        ok=bool(p.ok),
        available_kwh=str(d8(p.available_kwh)),
        max_exchangeable_kwh=str(d8(p.max_exchangeable_kwh)),
        rate_kwh_to_efhc=str(d8(p.rate_kwh_to_efhc)),
        detail=p.detail,
    )

# =============================================================================
# Пояснения «для чайника»:
#   • Эти ручки НИКОГДА не изменяют денежные/энергетические балансы — только чтение.
#   • Денежные операции (покупка панели, обмен и т.д.) вызываются из других роутов,
#     а фактические изменения выполняют сервисы (transactions_service и др.).
#   • Здесь нет и не будет суточных полей и эндпоинтов — только посекундный канон.
# =============================================================================
