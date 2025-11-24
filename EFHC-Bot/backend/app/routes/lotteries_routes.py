# -*- coding: utf-8 -*-
# backend/app/routes/lotteries_routes.py
# =============================================================================
# Назначение кода:
#   Пользовательские HTTP-ручки лотерей EFHC Bot: витрина активных лотерей,
#   статус лотереи, просмотр собственных билетов и покупка билетов.
#
# Канон/инварианты:
#   • Покупки билетов — денежные операции: СТРОГО требуем Idempotency-Key.
#   • Списания выполняет единый банковский сервис (через lottery_service),
#     порядок списаний — bonus_balance → main_balance (bonus-first).
#   • Пользователь не может уйти в минус; банк может (операции не блокируются).
#   • Никакой авто-выдачи NFT — только заявка (ручная обработка админом).
#
# ИИ-защита/самовосстановление:
#   • Все списки — курсорная пагинация (без OFFSET), устойчиво к нагрузкам.
#   • ETag для кэширования на клиенте; «мягкие» ошибки с понятными текстами.
#   • При отсутствии продвинутой аутентификации Telegram: читаем telegram_id
#     из заголовка X-Telegram-Id или из query (запасной режим).
#
# Запреты:
#   • Нет P2P и обратных конверсий; модуль не изменяет бизнес-правила канона.
#   • Нет «суточных» расчётов — лотереи не знают про генерацию энергии.
# =============================================================================

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.logging_core import get_logger
from backend.app.deps import get_db, encode_cursor, decode_cursor, make_etag, d8
from backend.app.services.lottery_service import (
    svc_list_active_lotteries,
    svc_get_lottery_status,
    svc_list_user_tickets,
    svc_buy_tickets,
)

logger = get_logger(__name__)
router = APIRouter(prefix="/lottery", tags=["lottery"])

# -----------------------------------------------------------------------------
# Pydantic-схемы (ответы API)
# -----------------------------------------------------------------------------

class CursorOut(BaseModel):
    value: Optional[str] = Field(None, description="Бинарно-безопасная строка курсора или null")

class LotteryItemOut(BaseModel):
    id: int
    title: str
    prize_type: str
    prize_value: Optional[str] = None
    ticket_price: str
    total_tickets: int
    tickets_sold: int
    status: str
    created_at: str  # ISO

class LotteryListOut(BaseModel):
    items: List[LotteryItemOut]
    next_cursor: CursorOut
    etag: str

class LotteryStatusOut(BaseModel):
    id: int
    title: str
    prize_type: str
    prize_value: Optional[str] = None
    ticket_price: str
    total_tickets: int
    tickets_sold: int
    status: str
    result: Optional[Dict[str, Any]] = None
    etag: str

class MyTicketsOut(BaseModel):
    ticket_ids: List[int]
    next_cursor: CursorOut
    etag: str

class BuyTicketsIn(BaseModel):
    quantity: int = Field(..., ge=1, le=100, description="Сколько билетов купить (1..100)")

class BuyTicketsOut(BaseModel):
    ok: bool
    purchased: int
    total_spent: str
    my_ticket_ids: List[int]
    tickets_sold: int

# -----------------------------------------------------------------------------
# Витрина активных лотерей (курсорно + ETag)
# -----------------------------------------------------------------------------

@router.get("/active", response_model=LotteryListOut, summary="Активные лотереи (курсорно)")
async def list_active_lotteries(
    limit: int = Query(20, ge=1, le=100),
    cursor: Optional[str] = Query(None, description="Строка курсора из предыдущего ответа"),
    db: AsyncSession = Depends(get_db),
) -> LotteryListOut:
    """
    Возвращает список активных лотерей с курсорной пагинацией.
    Время — только для курсора; OFFSET не используется.
    """
    cur_payload = decode_cursor(cursor) if cursor else None
    cur_tuple: Optional[Tuple[str, int]] = None
    if cur_payload and isinstance(cur_payload, dict) and "ts" in cur_payload and "id" in cur_payload:
        cur_tuple = (cur_payload["ts"], int(cur_payload["id"]))

    items, next_cur = await svc_list_active_lotteries(db=db, limit=limit, cursor=cur_tuple)

    # Формируем курсор следующей страницы
    next_cursor_str = None
    if next_cur:
        next_cursor_str = encode_cursor({"ts": next_cur[0], "id": next_cur[1]})

    # ETag: основан на последнем элементе и количестве элементов
    etag = make_etag({
        "kind": "lottery_active",
        "count": len(items),
        "last": items[-1]["id"] if items else None,
    })

    return LotteryListOut(
        items=[
            LotteryItemOut(
                id=i["id"],
                title=i["title"],
                prize_type=i["prize_type"],
                prize_value=i["prize_value"],
                ticket_price=str(d8(i["ticket_price"])),
                total_tickets=int(i["total_tickets"]),
                tickets_sold=int(i["tickets_sold"]),
                status=i["status"],
                created_at=i["created_at"].isoformat(),
            )
            for i in items
        ],
        next_cursor=CursorOut(value=next_cursor_str),
        etag=etag,
    )

# -----------------------------------------------------------------------------
# Статус конкретной лотереи (ETag)
# -----------------------------------------------------------------------------

@router.get("/{lottery_id}/status", response_model=LotteryStatusOut, summary="Статус лотереи")
async def get_lottery_status(
    lottery_id: int,
    db: AsyncSession = Depends(get_db),
) -> LotteryStatusOut:
    """
    Возвращает карточку лотереи и результат (если есть).
    """
    data = await svc_get_lottery_status(db=db, lottery_id=lottery_id)
    if not data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Лотерея не найдена.")

    etag = make_etag({
        "kind": "lottery_status",
        "id": data["id"],
        "tickets_sold": data["tickets_sold"],
        "status": data["status"],
        "winner": (data["result"] or {}).get("winning_ticket_id"),
    })

    return LotteryStatusOut(
        id=int(data["id"]),
        title=data["title"],
        prize_type=data["prize_type"],
        prize_value=data.get("prize_value"),
        ticket_price=str(d8(data["ticket_price"])),
        total_tickets=int(data["total_tickets"]),
        tickets_sold=int(data["tickets_sold"]),
        status=data["status"],
        result=data.get("result"),
        etag=etag,
    )

# -----------------------------------------------------------------------------
# Мои билеты (курсорно + ETag)
# -----------------------------------------------------------------------------

@router.get("/{lottery_id}/my-tickets", response_model=MyTicketsOut, summary="Мои билеты в лотерее (курсорно)")
async def list_my_tickets(
    lottery_id: int,
    limit: int = Query(50, ge=1, le=500),
    cursor: Optional[str] = Query(None),
    # Режим без сложной аутентификации: читаем Telegram ID из заголовка или query
    x_telegram_id: Optional[int] = Header(default=None, convert_underscores=False, alias="X-Telegram-Id"),
    telegram_id: Optional[int] = Query(None),
    db: AsyncSession = Depends(get_db),
) -> MyTicketsOut:
    """
    Возвращает список ID билетов пользователя в данной лотерее.
    Курсор — последний ticket_id.
    """
    tg_id = x_telegram_id or telegram_id
    if not tg_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Не указан Telegram ID пользователя.")

    cur_payload = decode_cursor(cursor) if cursor else None
    last_ticket_id_tuple: Optional[Tuple[int]] = None
    if cur_payload and "ticket_id" in cur_payload:
        last_ticket_id_tuple = (int(cur_payload["ticket_id"]),)

    ticket_ids, next_cur = await svc_list_user_tickets(
        db=db,
        lottery_id=lottery_id,
        telegram_id=int(tg_id),
        limit=limit,
        cursor=last_ticket_id_tuple,
    )

    next_cursor_str = encode_cursor({"ticket_id": next_cur[0]}) if next_cur else None
    etag = make_etag({
        "kind": "lottery_my",
        "lottery_id": lottery_id,
        "tg": int(tg_id),
        "last": ticket_ids[-1] if ticket_ids else None,
        "count": len(ticket_ids),
    })

    return MyTicketsOut(
        ticket_ids=[int(t) for t in ticket_ids],
        next_cursor=CursorOut(value=next_cursor_str),
        etag=etag,
    )

# -----------------------------------------------------------------------------
# Покупка билетов (денежная операция, Idempotency-Key обязателен)
# -----------------------------------------------------------------------------

@router.post("/{lottery_id}/buy", response_model=BuyTicketsOut, summary="Купить билеты (денежная операция)")
async def buy_tickets(
    lottery_id: int,
    payload: BuyTicketsIn,
    # Идемпотентность: ключ обязателен
    idempotency_key: Optional[str] = Header(default=None, convert_underscores=False, alias="Idempotency-Key"),
    # Telegram ID (временный запасной режим, пока нет полноценной аутентификации):
    x_telegram_id: Optional[int] = Header(default=None, convert_underscores=False, alias="X-Telegram-Id"),
    telegram_id: Optional[int] = Query(None),
    db: AsyncSession = Depends(get_db),
) -> BuyTicketsOut:
    """
    Денежная операция:
      • Требует заголовок Idempotency-Key (строго по канону).
      • Telegram ID читаем из X-Telegram-Id или из query (временный режим).
    Порядок списаний: bonus → main. Минус пользователю запрещён.
    """
    if not idempotency_key or not str(idempotency_key).strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Idempotency-Key обязателен для денежных операций."
        )

    tg_id = x_telegram_id or telegram_id
    if not tg_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Не указан Telegram ID пользователя.")

    try:
        result = await svc_buy_tickets(
            db=db,
            lottery_id=lottery_id,
            buyer_telegram_id=int(tg_id),
            quantity=int(payload.quantity),
            idempotency_key=str(idempotency_key).strip(),
        )
    except ValueError as ve:
        # Понятные бизнес-ошибки (недостаточно средств, продажи закрыты и т.п.)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(ve))
    except Exception as e:
        logger.exception("lottery.buy failed: %s", e)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Временная ошибка, попробуйте позже.")

    return BuyTicketsOut(
        ok=bool(result["ok"]),
        purchased=int(result["purchased"]),
        total_spent=str(result["total_spent"]),
        my_ticket_ids=[int(t) for t in result["my_ticket_ids"]],
        tickets_sold=int(result["tickets_sold"]),
    )

# =============================================================================
# Пояснения «для чайника»:
#   • /active — список активных лотерей. Курсор шифруется/дешифруется deps.encode/decode.
#   • /{id}/status — карточка лотереи, ETag помогает фронтенду кешировать ответ.
#   • /{id}/my-tickets — только ID билетов пользователя, курсорно, без OFFSET.
#   • /{id}/buy — денежная операция: обязателен Idempotency-Key.
#     Списания выполняются в сервисе через банк (bonus-first), минус пользователю запрещён.
#   • Telegram ID до внедрения полноценной auth читаем из X-Telegram-Id или ?telegram_id=.
# =============================================================================
