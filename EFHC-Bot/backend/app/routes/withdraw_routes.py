# -*- coding: utf-8 -*-
# backend/app/routes/withdraw_routes.py
# =============================================================================
# Назначение кода:
#   • Пользовательские ручки заявок на вывод EFHC (только EFHC; бонусы не выводятся).
#   • Денежные POST требуют Idempotency-Key (канон). Создание заявки мгновенно
#     холдирует сумму у пользователя → в Банк (через сервис), чтобы исключить
#     двойное расходование. Отмена делает рефанд из Банка.
#   • Списки заявок отдаются курсорно (без OFFSET) + ETag/If-None-Match.
#   • «Принудительная синхронизация» при открытии списка: авто-ремонт висячих
#     заявок через ensure_consistency().
#
# Канон/инварианты (строго):
#   • Пользователь не может уйти в минус (жёсткий запрет в сервисе).
#   • Банк может быть в минусе — операции не блокируются.
#   • Бонусный баланс не выводится; только основной.
#   • Никакого P2P и обратной конверсии EFHC→kWh.
#
# ИИ-защиты:
#   • Read-through идемпотентность: при повторном Idempotency-Key возвращается
#     ранее созданная заявка/операция без дублей.
#   • Backfill при открытии списка (ensure_consistency): авто-ремонт холдов/рефандов.
#   • ETag/If-None-Match: экономия трафика фронтенда при неизменившемся ответе.
#
# Запреты:
#   • Роуты не «выводят наружу» EFHC — только статусы и внутренняя бухгалтерия
#     холд/рефанд. Внешняя выплата — в админке (mark_paid/approve/reject).
# =============================================================================

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from pydantic import BaseModel, Field, conint
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config_core import get_settings
from backend.app.core.logging_core import get_logger
from backend.app.deps import get_db, d8  # централизованное округление Decimal(8)
from backend.app.services.withdraw_service import (
    request_withdraw,
    cancel_withdraw,
    list_user_withdraws,
    ensure_consistency,
    WithdrawPageDTO,
    WithdrawRequestDTO,
)

logger = get_logger(__name__)
settings = get_settings()
SCHEMA = getattr(settings, "DB_SCHEMA_CORE", "efhc_core")

router = APIRouter(prefix="/withdraw", tags=["withdraw"])

# =============================================================================
# Локальные курсор/ETag хелперы (стабильный порядок: created_at ASC, id ASC)
# =============================================================================

def _encode_cursor(ts: datetime, oid: int) -> str:
    import json
    return json.dumps({"ts": ts.astimezone(timezone.utc).isoformat(), "id": int(oid)}, separators=(",", ":"))

def _decode_cursor(cursor: Optional[str]) -> Tuple[Optional[datetime], Optional[int]]:
    if not cursor:
        return None, None
    try:
        import json
        data = json.loads(cursor)
        raw_ts = data.get("ts")
        rid = data.get("id")
        ts = None
        if raw_ts:
            ts = datetime.fromisoformat(str(raw_ts).replace("Z", "+00:00")).astimezone(timezone.utc)
        return ts, (int(rid) if rid is not None else None)
    except Exception:
        return None, None

def _build_etag(payload: Dict[str, Any]) -> str:
    import hashlib, json
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()

# =============================================================================
# Pydantic-схемы
# =============================================================================

class WithdrawCreateIn(BaseModel):
    user_id: conint(strict=True, ge=1)
    amount: str = Field(..., description="Сумма EFHC к выводу (строка с 8 знаками)")

class WithdrawOut(BaseModel):
    id: int
    user_id: int
    amount: str
    status: str
    created_at: str
    updated_at: str

class WithdrawDetailOut(WithdrawOut):
    client_idk: str
    hold_done: bool
    refund_done: bool
    payout_ref: Optional[str] = None

class WithdrawPageOut(BaseModel):
    items: List[WithdrawOut]
    next_cursor: Optional[str]

# =============================================================================
# POST /withdraw/request — создать заявку (денежный POST → требуется Idempotency-Key)
# =============================================================================

@router.post("/request", response_model=WithdrawDetailOut, summary="Создать заявку на вывод EFHC (холд средств)")
async def post_withdraw_request(
    body: WithdrawCreateIn,
    db: AsyncSession = Depends(get_db),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
) -> WithdrawDetailOut:
    """
    Что делает:
      • Создаёт заявку REQUESTED и сразу холдирует сумму: списывает EFHC у пользователя в Банк.
    Важно:
      • Требуется заголовок Idempotency-Key (строго канон).
      • Бонусы не выводятся, только основной баланс; минус у пользователя запрещён.
    Исключения:
      • 400 — нет Idempotency-Key или некорректная сумма.
      • 409/500 — при сбоях базы/идемпотентности (вернётся стабильный ответ при повторе).
    """
    # Канон: денежный POST → Idempotency-Key обязателен
    if not idempotency_key or not idempotency_key.strip():
        raise HTTPException(status_code=400, detail="Idempotency-Key header is required for monetary operations.")

    # Безопасный парсинг суммы
    try:
        amt = d8(Decimal(body.amount))
    except (InvalidOperation, ValueError):
        raise HTTPException(status_code=400, detail="amount must be a valid decimal string with up to 8 digits after the point.")

    try:
        dto: WithdrawRequestDTO = await request_withdraw(
            db,
            user_id=int(body.user_id),
            amount=amt,
            client_idk=str(idempotency_key).strip(),
        )
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    except RuntimeError as re:
        # Недостаточно средств у пользователя или временная ошибка холда
        raise HTTPException(status_code=409, detail=str(re))
    except Exception as e:
        logger.error("post_withdraw_request failed: %s", e)
        raise HTTPException(status_code=500, detail="Временная ошибка. Повторите позже.")

    return WithdrawDetailOut(
        id=dto.id,
        user_id=dto.user_id,
        amount=str(d8(dto.amount)),
        status=dto.status,
        client_idk=dto.client_idk,
        hold_done=bool(dto.hold_done),
        refund_done=bool(dto.refund_done),
        payout_ref=dto.payout_ref,
        created_at=dto.created_at,
        updated_at=dto.updated_at,
    )

# =============================================================================
# POST /withdraw/{request_id}/cancel — отменить заявку (денежный POST → Idempotency-Key)
# =============================================================================

@router.post("/{request_id}/cancel", response_model=WithdrawDetailOut, summary="Отменить заявку (рефанд при наличии холда)")
async def post_withdraw_cancel(
    request_id: int,
    user_id: int,
    db: AsyncSession = Depends(get_db),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
) -> WithdrawDetailOut:
    """
    Что делает:
      • Отменяет заявку (если не PAID) и выполняет рефанд из Банка, если холд был.
    Важно:
      • Денежный POST → канон требует Idempotency-Key. Он используется как внешний
        ключ идемпотентности операции отмены, а фактический банковский ключ рефанда
        основан на client_idk заявки (read-through, без дублей).
    """
    if not idempotency_key or not idempotency_key.strip():
        raise HTTPException(status_code=400, detail="Idempotency-Key header is required for monetary operations.")

    try:
        dto: WithdrawRequestDTO = await cancel_withdraw(
            db,
            request_id=int(request_id),
            user_id=int(user_id),
            client_idk=str(idempotency_key).strip(),  # сервис использует внутренний client_idk заявки
        )
    except ValueError as ve:
        raise HTTPException(status_code=404, detail=str(ve))
    except RuntimeError as re:
        raise HTTPException(status_code=409, detail=str(re))
    except Exception as e:
        logger.error("post_withdraw_cancel failed: %s", e)
        raise HTTPException(status_code=500, detail="Временная ошибка. Повторите позже.")

    return WithdrawDetailOut(
        id=dto.id,
        user_id=dto.user_id,
        amount=str(d8(dto.amount)),
        status=dto.status,
        client_idk=dto.client_idk,
        hold_done=bool(dto.hold_done),
        refund_done=bool(dto.refund_done),
        payout_ref=dto.payout_ref,
        created_at=dto.created_at,
        updated_at=dto.updated_at,
    )

# =============================================================================
# GET /withdraw/list/{user_id} — курсорный список + ETag + принудительный backfill
# =============================================================================

@router.get("/list/{user_id}", response_model=WithdrawPageOut, summary="Список заявок пользователя (курсорно)")
async def get_withdraw_list(
    user_id: int,
    limit: int = 100,
    cursor: Optional[str] = None,
    status_filter: Optional[str] = None,
    backfill: bool = True,
    if_none_match: Optional[str] = Header(None, convert_underscores=True),
    db: AsyncSession = Depends(get_db),
) -> WithdrawPageOut:
    """
    Что делает:
      • Возвращает страницу заявок пользователя (ORDER BY created_at,id), next_cursor.
    ИИ-самовосстановление:
      • При backfill=True выполняет ensure_consistency() перед выдачей — авто-ремонт
        «висячих» холдов/рефандов в последних записях.
    ETag:
      • При совпадении If-None-Match возвращается 304 Not Modified.
    """
    if backfill:
        try:
            stats = await ensure_consistency(db, scan_minutes=240, batch_limit=200)
            if (stats.get("auto_fixed_hold") or 0) or (stats.get("auto_fixed_refund") or 0):
                logger.info("withdraw backfill auto-fixed: %s", stats)
        except Exception as e:
            logger.warning("ensure_consistency skipped: %s", e)

    try:
        page: WithdrawPageDTO = await list_user_withdraws(
            db,
            user_id=int(user_id),
            limit=int(limit),
            cursor=cursor,
            status_filter=(status_filter or None),
        )
    except Exception as e:
        logger.error("get_withdraw_list failed: %s", e)
        raise HTTPException(status_code=500, detail="Временная ошибка при чтении заявок.")

    payload = {
        "scope": "withdraw_list",
        "user_id": int(user_id),
        "next_cursor": page.next_cursor or "",
        "items": [
            {
                "id": i.id,
                "user_id": i.user_id,
                "amount": str(d8(i.amount)),
                "status": i.status,
                "created_at": i.created_at,
                "updated_at": i.updated_at,
            }
            for i in page.items
        ],
    }
    etag = _build_etag(payload)

    if if_none_match and if_none_match.strip() == etag:
        # Ничего не изменилось — экономим трафик
        raise HTTPException(status_code=status.HTTP_304_NOT_MODIFIED, detail="Not Modified")

    resp = Response(
        content=WithdrawPageOut(
            items=[
                WithdrawOut(
                    id=i.id,
                    user_id=i.user_id,
                    amount=str(d8(i.amount)),
                    status=i.status,
                    created_at=i.created_at,
                    updated_at=i.updated_at,
                ) for i in page.items
            ],
            next_cursor=page.next_cursor,
        ).model_dump_json(),
        status_code=status.HTTP_200_OK,
        media_type="application/json",
        headers={"ETag": etag},
    )
    return resp  # type: ignore[return-value]

# =============================================================================
# GET /withdraw/{request_id} — детали заявки (для UI/обновления статуса)
# =============================================================================

@router.get("/{request_id}", response_model=WithdrawDetailOut, summary="Детали заявки на вывод")
async def get_withdraw_detail(
    request_id: int,
    if_none_match: Optional[str] = Header(None, convert_underscores=True),
    db: AsyncSession = Depends(get_db),
) -> WithdrawDetailOut:
    """
    Что делает:
      • Возвращает текущее состояние заявки. Денежных эффектов нет.
    """
    q = text(f"""
        SELECT id, user_id, amount, status, client_idk, hold_done, refund_done, payout_ref, created_at, updated_at
        FROM {SCHEMA}.withdraw_requests
        WHERE id = :rid
        LIMIT 1
    """)
    r = await db.execute(q, {"rid": int(request_id)})
    row = r.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Заявка не найдена.")

    dto = WithdrawRequestDTO(
        id=int(row[0]),
        user_id=int(row[1]),
        amount=d8(row[2]),
        status=str(row[3]),
        client_idk=str(row[4]),
        hold_done=bool(row[5]),
        refund_done=bool(row[6]),
        payout_ref=(str(row[7]) if row[7] else None),
        created_at=row[8].astimezone(timezone.utc).isoformat(),
        updated_at=row[9].astimezone(timezone.utc).isoformat(),
    )

    payload = {
        "scope": "withdraw_detail",
        "id": dto.id,
        "user_id": dto.user_id,
        "amount": str(d8(dto.amount)),
        "status": dto.status,
        "client_idk": dto.client_idk,
        "hold_done": bool(dto.hold_done),
        "refund_done": bool(dto.refund_done),
        "payout_ref": dto.payout_ref or "",
        "updated_at": dto.updated_at,
    }
    etag = _build_etag(payload)
    if if_none_match and if_none_match.strip() == etag:
        raise HTTPException(status_code=status.HTTP_304_NOT_MODIFIED, detail="Not Modified")

    resp = Response(
        content=WithdrawDetailOut(
            id=dto.id,
            user_id=dto.user_id,
            amount=str(d8(dto.amount)),
            status=dto.status,
            client_idk=dto.client_idk,
            hold_done=bool(dto.hold_done),
            refund_done=bool(dto.refund_done),
            payout_ref=dto.payout_ref,
            created_at=dto.created_at,
            updated_at=dto.updated_at,
        ).model_dump_json(),
        status_code=status.HTTP_200_OK,
        media_type="application/json",
        headers={"ETag": etag},
    )
    return resp  # type: ignore[return-value]

# =============================================================================
# Пояснения «для чайника»:
#  • Зачем требовать Idempotency-Key на POST?
#    Это страхует от дублей при повторах запросов (сети/клиент). С тем же ключом
#    сервер возвращает один и тот же результат, не создавая новую операцию.
#  • Почему при открытии списка идёт ensure_consistency()?
#    Если в прошлый раз упал холд/рефанд, авто-ремонт подтянет состояние до
#    корректного без участия пользователя.
#  • Почему нет «внешней выплаты» в этих ручках?
#    Эти ручки управляют только внутренними статусами и бухгалтерией EFHC.
#    Внешняя выплата — в админке (approve/reject/mark_paid) и/или интеграции.
# =============================================================================
