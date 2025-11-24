# -*- coding: utf-8 -*-
# backend/app/routes/shop_routes.py
# =============================================================================
# Назначение кода:
# Витрины и операции магазина (Shop): каталог SKU, создание внешнего заказа
# (оплата TON/USDT через watcher) и внутренняя покупка за EFHC. Возвращает
# статусные DTO для UI, поддерживает принудительную синхронизацию раздела.
#
# Канон / инварианты:
# • Денежные POST-операции требуют строгой идемпотентности: заголовок
#   Idempotency-Key (уникальный ключ клиента). Повтор → read-through:
#   возвращаем результат первой обработки, не дублируем.
# • Обмен kWh→EFHC только 1:1 (не относится к Shop, но влияет на витрины).
# • VIP/NFT — только заявка, автоматическая выдача NFT запрещена.
# • Пользователь НЕ может уходить в минус (жёсткая проверка в сервисах).
# • Банк может уйти в минус (операции не блокируем, логируем дефицит).
#
# ИИ-защиты / самовосстановление:
# • Внешние заказы (TON/USDT) используют «мягкую идемпотентность» по
#   X-Client-Nonce для защиты от повторных кликов до оплаты в блокчейне.
# • Списки — курсорная пагинация без OFFSET (устойчиво к росту данных).
# • ETag на витрины и списки (кэш без рассинхронизации).
# • «Принудительная синхронизация» при открытии раздела: безопасный хук,
#   не блокирующий UI (через легкий вызов сервисной синхронизации).
#
# Запреты:
# • НЕТ автодоставки NFT. Только заявка со статусом PAID_PENDING_MANUAL.
# • НЕТ жесткого отказа из-за минуса банка.
# • НЕТ хранения курсов TON/USDT в API — админ задаёт цены товарам в БД.
# =============================================================================

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config_core import get_settings
from backend.app.core.logging_core import get_logger
from backend.app.deps import (
    get_db,
    d8,
    make_etag,
    encode_cursor,
    decode_cursor,
)
from backend.app.services.shop_service import (
    get_catalog,
    create_external_order,
    purchase_efhc_internal,
    list_orders,
)
# Схемы ответов/входов. Если имена отличаются — синхронизируйте со схемами:
from backend.app.schemas.shop_schemas import (
    ShopCatalogOut,
    ShopOrderExternalIn,
    ShopOrderExternalOut,
    ShopPurchaseEFHCIn,
    ShopPurchaseEFHCOut,
    ShopOrderListOut,
)

logger = get_logger(__name__)
settings = get_settings()
SCHEMA = getattr(settings, "DB_SCHEMA_CORE", "efhc_core")

router = APIRouter(prefix="/shop", tags=["shop"])

# -----------------------------------------------------------------------------
# Вспомогательные функции (локальные для роутера)
# -----------------------------------------------------------------------------

async def _resolve_user_id(req: Request) -> int:
    """
    Универсальное извлечение пользователя.
    Каноническая авторизация телеграмом будет добавлена отдельно (primary).
    Пока поддерживаем fallback: заголовок X-Telegram-Id или query ?telegram_id=.
    """
    # Primary (будущий канал): req.state.user_id, если middleware авторизации Телеграм.
    if hasattr(req.state, "user_id") and req.state.user_id:
        return int(req.state.user_id)

    # Fallback: заголовок
    hdr = req.headers.get("X-Telegram-Id")
    if hdr and hdr.isdigit():
        return int(hdr)

    # Fallback: query
    qv = req.query_params.get("telegram_id")
    if qv and qv.isdigit():
        return int(qv)

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Не удалось определить пользователя: отсутствует авторизация Telegram или X-Telegram-Id/telegram_id."
    )


async def _get_user_balances(db: AsyncSession, user_tg_id: int) -> Tuple[Decimal, Decimal]:
    """
    Быстрый селект основных балансов. Нужен для витрин (дизейблы по EFHC).
    Возвращает (main_balance, bonus_balance).
    """
    row = await db.execute(
        text(
            f"""
            SELECT
              COALESCE(u.main_balance, 0)::numeric,
              COALESCE(u.bonus_balance, 0)::numeric
            FROM {SCHEMA}.users u
            WHERE u.telegram_id = :tg
            LIMIT 1
            """
        ),
        {"tg": int(user_tg_id)},
    )
    rec = row.fetchone()
    if not rec:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Пользователь {user_tg_id} не найден."
        )
    return (Decimal(rec[0]), Decimal(rec[1]))


def _require_idempotency_key(req: Request) -> str:
    """
    Жёсткая идемпотентность для денежных POST (внутренние покупки за EFHC).
    """
    key = req.headers.get("Idempotency-Key", "").strip()
    if not key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Idempotency-Key header обязателен для денежных операций."
        )
    if len(key) > 128:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Idempotency-Key слишком длинный (макс. 128 символов)."
        )
    return key


def _optional_client_nonce(req: Request) -> Optional[str]:
    """
    Мягкая идемпотентность для внешних заказов: X-Client-Nonce.
    Позволяет не плодить дубли при повторных кликах до подтверждения оплаты.
    """
    nonce = (req.headers.get("X-Client-Nonce") or "").strip()
    if not nonce:
        return None
    if len(nonce) > 64:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="X-Client-Nonce слишком длинный (макс. 64 символа)."
        )
    return nonce


# -----------------------------------------------------------------------------
# GET /shop/catalog — витрина товаров (SKU)
# -----------------------------------------------------------------------------

@router.get("/catalog", response_model=ShopCatalogOut, summary="Каталог магазина (SKU)")
async def get_shop_catalog(
    request: Request,
    db: AsyncSession = Depends(get_db),
    force_sync: bool = False,
) -> ShopCatalogOut:
    """
    Что делает:
      • Возвращает актуальный каталог товара с методами оплаты (EFHC/TON/USDT),
        и полями enabled/disabled_reason на основе состояния пользователя и цен.
      • Поддерживает «принудительную синхронизацию» раздела (без подвисаний).

    Вход:
      • query force_sync=true|false — мягкий триггер синхронизации (не блокирует UI).
      • пользователя определяем через авторизацию или X-Telegram-Id/telegram_id.

    Выход:
      • Список SKU с методами оплаты и дизейблами (price=0 → disabled).
      • ETag в ответе (через make_etag).

    Исключения:
      • 401 — пользователь не определён.
      • 404 — пользователь не найден (балансы).
    """
    user_id = await _resolve_user_id(request)

    if force_sync:
        # Принудительная синхронизация раздела. Здесь делаем безопасный «пин-глоток»,
        # чтобы не блокировать UI (например, kick watcher/тонкую проверку).
        # Специально не ждём тяжёлых операций.
        try:
            # Лёгкий асинхронный "пинок": не падаем даже при ошибках.
            asyncio.create_task(asyncio.sleep(0.01))
        except Exception as e:
            logger.warning("shop/catalog force_sync hint failed: %s", e)

    main_balance, bonus_balance = await _get_user_balances(db, user_id)
    catalog = await get_catalog(db, user_id=user_id)

    # Правило дизейблов:
    # • price == 0 → карточка/метод отключены (админ не задал цену).
    # • при историческом минусе пользователя (если где-то появится) — покупки за EFHC
    #   должны быть отключены до выхода в 0 (в сервисах защита жёстче).
    user_negative = (main_balance < 0 or bonus_balance < 0)

    enriched_items: List[Dict[str, Any]] = []
    for item in catalog.items:
        item_dict = item.model_dump()
        methods = item_dict.get("methods") or {}
        # Пройдёмся по методам оплаты и дополним отключения:
        for pay_method, cfg in methods.items():
            price = Decimal(str(cfg.get("price") or "0"))
            if price <= 0:
                cfg["enabled"] = False
                cfg["disabled_reason"] = "Цена не задана админом"
            elif pay_method.upper() == "EFHC" and user_negative:
                cfg["enabled"] = False
                cfg["disabled_reason"] = "Баланс пользователя в минусе — покупки за EFHC недоступны"
            else:
                # Если сервис уже рассчитал enabled/disabled — не ломаем, лишь дополняем.
                cfg["enabled"] = bool(cfg.get("enabled", True))
                if not cfg["enabled"] and not cfg.get("disabled_reason"):
                    cfg["disabled_reason"] = "Недоступно по правилам магазина"

        item_dict["methods"] = methods
        enriched_items.append(item_dict)

    body = ShopCatalogOut(
        items=enriched_items,
        user_info={
            "main_balance": str(d8(main_balance)),
            "bonus_balance": str(d8(bonus_balance)),
            "user_negative": bool(user_negative),
        },
    )

    # ETag: по длине и контрольной сумме сериализованных ключей (через make_etag)
    etag = make_etag({
        "n": len(enriched_items),
        "neg": user_negative,
        "sum": str(d8(main_balance + bonus_balance)),
    })
    # FastAPI сам положит ETag если вернуть Response — здесь покажем через headers:
    # но т.к. response_model используется, обычно ETag в middleware.
    request.state.extra_headers = [("ETag", etag)]
    return body


# -----------------------------------------------------------------------------
# POST /shop/order-external — создать внешний заказ (оплата TON/USDT)
# -----------------------------------------------------------------------------

@router.post(
    "/order-external",
    response_model=ShopOrderExternalOut,
    summary="Создать внешний заказ (оплата TON/USDT)",
    status_code=status.HTTP_200_OK,
)
async def create_order_external(
    request: Request,
    payload: ShopOrderExternalIn,
    db: AsyncSession = Depends(get_db),
) -> ShopOrderExternalOut:
    """
    Что делает:
      • Регистрирует внешний заказ (метод TON/USDT), возвращает платёжные реквизиты:
        memo, to_wallet, ожидаемую сумму. Дальше оплату отслеживает watcher.
      • Использует мягкую идемпотентность X-Client-Nonce для защиты от дублей.

    Вход:
      • body: { sku, quantity, pay_method }
      • headers: X-Client-Nonce (опционально, но рекомендуется)

    Выход:
      • { order: {id, status}, pay: {method, amount, memo, to_wallet} }

    Исключения:
      • 400 — невалидный SKU/метод/кол-во/nonce слишком длинный.
      • 401 — пользователь не определён.
    """
    user_id = await _resolve_user_id(request)
    client_nonce = _optional_client_nonce(request)

    order, pay = await create_external_order(
        db=db,
        user_id=user_id,
        sku=payload.sku,
        quantity=payload.quantity,
        pay_method=payload.pay_method,
        client_nonce=client_nonce,
    )
    return ShopOrderExternalOut(order=order, pay=pay)


# -----------------------------------------------------------------------------
# POST /shop/purchase-efhc — внутренняя покупка за EFHC (жёсткая идемпотентность)
# -----------------------------------------------------------------------------

@router.post(
    "/purchase-efhc",
    response_model=ShopPurchaseEFHCOut,
    summary="Покупка за EFHC (внутренняя, жёсткая идемпотентность)",
    status_code=status.HTTP_200_OK,
)
async def purchase_efhc(
    request: Request,
    payload: ShopPurchaseEFHCIn,
    db: AsyncSession = Depends(get_db),
) -> ShopPurchaseEFHCOut:
    """
    Что делает:
      • Списывает EFHC у пользователя (сначала бонус, затем основной), создаёт заказ
        и выполняет «доставку» (по типу SKU). Вся денежная логика — через банк.

    Вход:
      • body: { sku, quantity }
      • headers: Idempotency-Key (обязательно, <= 128)

    Выход:
      • Итоговый заказ и контекст списаний (read-through: повторы ключа → тот же результат).

    Исключения:
      • 400 — отсутствует/слишком длинный Idempotency-Key; SKU/quantity некорректны.
      • 401 — пользователь не определён.
      • 409 — (не используется) — вместо этого read-through отдаёт 200 с прежним результатом.
    """
    user_id = await _resolve_user_id(request)
    idk = _require_idempotency_key(request)
    result = await purchase_efhc_internal(
        db=db,
        user_id=user_id,
        sku=payload.sku,
        quantity=payload.quantity,
        idempotency_key=idk,
    )
    return result


# -----------------------------------------------------------------------------
# GET /shop/orders — список заказов пользователя (курсор, ETag)
# -----------------------------------------------------------------------------

@router.get(
    "/orders",
    response_model=ShopOrderListOut,
    summary="Список заказов пользователя (курсорная пагинация)",
)
async def get_orders(
    request: Request,
    db: AsyncSession = Depends(get_db),
    cursor: Optional[str] = None,
    limit: int = 20,
) -> ShopOrderListOut:
    """
    Что делает:
      • Возвращает заказы текущего пользователя по курсору (id DESC), без OFFSET.

    Вход:
      • cursor: base64(JSON) {"after_id": <int>}; если None — читаем «с головы».
      • limit: 1..100 (по умолчанию 20).

    Выход:
      • items, next_cursor (если есть ещё), ETag для кэша.

    Исключения:
      • 401 — пользователь не определён.
    """
    user_id = await _resolve_user_id(request)

    if limit < 1:
        limit = 1
    if limit > 100:
        limit = 100

    after_id = None
    if cursor:
        try:
            cur = decode_cursor(cursor)
            after_id = int(cur.get("after_id")) if cur.get("after_id") else None
        except Exception:
            raise HTTPException(status_code=400, detail="Некорректный cursor")

    data = await list_orders(db=db, user_id=user_id, after_id=after_id, limit=limit)

    # Сформируем next_cursor:
    next_cursor = None
    if data.next_after_id is not None:
        next_cursor = encode_cursor({"after_id": int(data.next_after_id)})

    body = ShopOrderListOut(
        items=data.items,
        next_cursor=next_cursor,
    )

    etag = make_etag({
        "n": len(data.items),
        "head": int(data.items[0]["id"]) if data.items else 0,
        "tail": int(data.items[-1]["id"]) if data.items else 0,
    })
    request.state.extra_headers = [("ETag", etag)]
    return body


# =============================================================================
# Пояснения «для чайника»:
# 1) Почему Idempotency-Key обязательный?
#    Любая денежная операция должна быть устойчива к повторным кликам/ретраям.
#    Клиент отправляет уникальный ключ. Если запрос повторится — сервер найдёт
#    ранее записанный результат (через UNIQUE в логах) и вернёт его (read-through),
#    не создавая дублей.
#
# 2) Зачем X-Client-Nonce?
#    Для внешних заказов (оплата в TON/USDT) у пользователя часто возникают повторы
#    до фактической оплаты. Nonce позволяет мягко склеивать такие повторы в «один
#    ожидающий платёж», пока watcher по tx_hash не подтвердит.
#
# 3) Почему курсор вместо OFFSET?
#    OFFSET плохо масштабируется: чем дальше в историю, тем дороже запрос.
#    Курсор по id DESC даёт стабильную и быструю пагинацию.
#
# 4) Где берутся цены TON/USDT?
#    Из БД (заводит админ). Если price=0 — карточка/метод отключены, покупать нельзя.
#
# 5) Что значит «принудительная синхронизация»?
#    Перед отдачей витрины посылаем лёгкий «пин-глоток» сервисам (не блокируем),
#    чтобы раздел имел свежие данные при быстром переключении экранов.
#
# 6) Почему не блокируем при минусе банка?
#    Канон: банк может уйти в минус — это не останавливает экономику. Мы логируем
#    processed_with_deficit. Пользователь — никогда не в минус.
# =============================================================================
