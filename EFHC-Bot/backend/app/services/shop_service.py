# -*- coding: utf-8 -*-
# backend/app/services/shop_service.py
# =============================================================================
# Назначение кода:
# Сервис магазина EFHC: каталожные позиции (сидер), витрины, создание заказов,
# генерация MEMO для внешних платежей (TON/USDT), внутренняя оплата EFHC
# (списание: сначала bonus, затем main), интеграция с Банком и Вотчером.
#
# Канон/инварианты:
# • Обмен только kWh→EFHC (1:1), обратной конверсии нет.
# • P2P запрещено. Любые движения EFHC — только «банк ↔ пользователь».
# • NFT — только заявка и ручная обработка (никакой автодоставки NFT).
# • EFHC-пакеты (10…1000) оплачиваются внешне (TON/USDT), за EFHC — запрещено.
# • Денежные POST — ОБЯЗАТЕЛЕН Idempotency-Key / client_nonce.
# • Пользователь не может уйти в минус. Банк может (дефицит — не блокирует операции).
#
# ИИ-защита/самовосстановление:
# • Идемпотентность денежной логики: read-through (конфликт idempotency_key => вернуть уже
#   зафиксированный результат из логов); повторные вызовы безопасны.
# • Лог-ориентированность: все заказы и MEMO фиксируются в БД; вотчер подхватывает оплату
#   по tx_hash и обновляет статусы.
# • Витрина «умная»: отдаёт флаги доступности и «disabled_reason» для UI, чтобы кнопки
#   автоматически деактивировались при минусе/недостатке средств/неподдерживаемом методе.
#
# Запреты:
# • Никаких автодоставок NFT. Только создание заявки PAID_PENDING_MANUAL.
# • EFHC-пакеты нельзя оплачивать EFHC (покупать EFHC за EFHC бессмысленно).
# • Никаких скрытых начислений, минуя Банк.
# =============================================================================

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config_core import get_settings
from backend.app.core.logging_core import get_logger
from backend.app.deps import d8, encode_cursor, decode_cursor
from backend.app.services import transactions_service as bank

logger = get_logger(__name__)
settings = get_settings()
SCHEMA = getattr(settings, "DB_SCHEMA_CORE", "efhc_core")

# ---- Канонические типы/статусы ------------------------------------------------

ITEM_TYPE_EFHC_PACKAGE = "EFHC_PACKAGE"      # пакеты EFHC: 10..1000 EFHC (внешняя оплата)
ITEM_TYPE_NFT_VIP = "NFT_VIP"                # VIP NFT (TON/USDT/EFHC)
ITEM_TYPE_VIRTUAL = "VIRTUAL"                # прочие виртуальные товары (за EFHC внутри)

PAY_TON = "TON"
PAY_USDT = "USDT"
PAY_EFHC = "EFHC"

ORDER_STATUS_PENDING = "PENDING"                   # заказ создан, ждём внешнюю оплату
ORDER_STATUS_PAID_AUTO = "PAID_AUTO"               # оплачен автоматически (внешний платёж подтверждён вотчером / авто-логика)
ORDER_STATUS_PAID_PENDING_MANUAL = "PAID_PENDING_MANUAL"  # оплачен, ожидание ручной выдачи (NFT)
ORDER_STATUS_CANCELLED = "CANCELLED"
ORDER_STATUS_REJECTED = "REJECTED"

# ---- Вспомогательные DTO ------------------------------------------------------

@dataclass
class ShopItemDTO:
    id: int
    code: str
    title: str
    item_type: str
    is_active: bool
    price_efhc: Optional[Decimal]
    price_ton: Optional[Decimal]
    price_usdt: Optional[Decimal]
    pay_currencies: List[str]
    extra_json: Dict[str, Any]

@dataclass
class OrderDTO:
    id: int
    user_id: int
    item_id: int
    status: str
    payment_method: Optional[str]
    memo: Optional[str]
    tx_hash: Optional[str]
    created_at: datetime

# ---- Утилиты ------------------------------------------------------------------

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)

def _pay_methods_from_row(row) -> List[str]:
    # pay_currencies хранится как массив TEXT[] или JSON — нормализуем к List[str]
    val = row["pay_currencies"]
    if val is None:
        return []
    if isinstance(val, list):
        return [str(x) for x in val]
    if isinstance(val, str):
        # на случай JSON-строки
        try:
            import json
            arr = json.loads(val)
            return [str(x) for x in arr] if isinstance(arr, list) else []
        except Exception:
            return []
    return []

# ---- Сидер каталога под список EFHC-пакетов и VIP ----------------------------

async def seed_default_items(db: AsyncSession) -> Dict[str, Any]:
    """
    Идемпотентный сидер: создаёт (если нет) базовые карточки товаров.
    Цены НЕ жёсткие — будут отредактированы в админ-панели.
    """
    rockets = [10, 50, 100, 200, 300, 400, 500, 1000]
    inserts = 0
    updated = 0  # зарезервировано под будущие обновления

    # EFHC-пакеты (внешняя оплата TON/USDT)
    for qty in rockets:
        code = f"EFHC_{qty}"
        title = f"{qty} EFHC"
        pay_currencies = [PAY_TON, PAY_USDT]  # только внешние методы
        q = text(f"""
            INSERT INTO {SCHEMA}.shop_items
              (code, title, item_type, is_active, price_efhc, price_ton, price_usdt,
               pay_currencies, extra_json, created_at, updated_at)
            SELECT :code, :title, :itype, TRUE, NULL, NULL, NULL, :pays, :extra::jsonb, NOW(), NOW()
            WHERE NOT EXISTS (
                SELECT 1 FROM {SCHEMA}.shop_items WHERE code = :code
            )
            RETURNING id
        """)
        res = await db.execute(q, {
            "code": code,
            "title": title,
            "itype": ITEM_TYPE_EFHC_PACKAGE,
            "pays": pay_currencies,
            "extra": '{"quantity_efhc": %d}' % qty,
        })
        if res.fetchone():
            inserts += 1

    # VIP NFT — три карточки товара: за TON, USDT, EFHC
    vip_variants = [
        ("VIP_NFT_TON",  "VIP NFT (TON)",  [PAY_TON],  None, None),
        ("VIP_NFT_USDT", "VIP NFT (USDT)", [PAY_USDT], None, None),
        ("VIP_NFT_EFHC", "VIP NFT (EFHC)", [PAY_EFHC], None, None),
    ]
    for code, title, pays, price_ton, price_usdt in vip_variants:
        q = text(f"""
            INSERT INTO {SCHEMA}.shop_items
              (code, title, item_type, is_active, price_efhc, price_ton, price_usdt,
               pay_currencies, extra_json, created_at, updated_at)
            SELECT :code, :title, :itype, TRUE, NULL, :pt, :pu, :pays, :extra::jsonb, NOW(), NOW()
            WHERE NOT EXISTS (
                SELECT 1 FROM {SCHEMA}.shop_items WHERE code = :code
            )
            RETURNING id
        """)
        res = await db.execute(q, {
            "code": code,
            "title": title,
            "itype": ITEM_TYPE_NFT_VIP,
            "pt": price_ton,
            "pu": price_usdt,
            "pays": pays,
            "extra": '{"nft_collection": "%s"}' % getattr(settings, "TON_NFT_COLLECTION", ""),
        })
        if res.fetchone():
            inserts += 1

    await db.commit()
    return {"inserted": inserts, "updated": updated}

# ---- Балансы пользователя -----------------------------------------------------

async def _get_user_balances(db: AsyncSession, user_id: int) -> Tuple[Decimal, Decimal]:
    q = text(f"""
        SELECT COALESCE(main_balance,0) AS main_balance,
               COALESCE(bonus_balance,0) AS bonus_balance
        FROM {SCHEMA}.users WHERE id = :uid
        LIMIT 1
    """)
    row = (await db.execute(q, {"uid": user_id})).mappings().first()
    if not row:
        raise ValueError(f"User id={user_id} not found")
    return d8(row["main_balance"]), d8(row["bonus_balance"])

# ---- Загрузка каталога --------------------------------------------------------

async def _fetch_items(db: AsyncSession) -> List[ShopItemDTO]:
    q = text(f"""
        SELECT id, code, title, item_type, is_active,
               price_efhc, price_ton, price_usdt, pay_currencies, extra_json
        FROM {SCHEMA}.shop_items
        WHERE COALESCE(is_active, TRUE) = TRUE
        ORDER BY id ASC
    """)
    rows = (await db.execute(q)).mappings().all()
    items: List[ShopItemDTO] = []
    for r in rows:
        items.append(ShopItemDTO(
            id=r["id"],
            code=r["code"],
            title=r["title"],
            item_type=r["item_type"],
            is_active=bool(r["is_active"]),
            price_efhc=(d8(r["price_efhc"]) if r["price_efhc"] is not None else None),
            price_ton=(d8(r["price_ton"]) if r["price_ton"] is not None else None),
            price_usdt=(d8(r["price_usdt"]) if r["price_usdt"] is not None else None),
            pay_currencies=_pay_methods_from_row(r),
            extra_json=r["extra_json"] or {},
        ))
    return items

# ---- Витрина магазина для пользователя ---------------------------------------

async def list_shop_for_user(db: AsyncSession, user_id: int) -> Dict[str, Any]:
    """
    Витрина магазина для пользователя:
      • отдаёт весь активный каталог товаров;
      • по каждой карточке товара рассчитывает доступность кнопок по методам оплаты;
      • для EFHC-покупок — проверяет, хватает ли (bonus+main) и не в «минусе» ли пользователь.
    """
    main, bonus = await _get_user_balances(db, user_id)
    total_efhc = d8(main + bonus)

    items = await _fetch_items(db)

    def check_efhc_afford(item: ShopItemDTO) -> Tuple[bool, str]:
        if item.item_type == ITEM_TYPE_EFHC_PACKAGE:
            # EFHC-пакеты нельзя покупать за EFHC
            return (False, "efhc_not_supported_for_packages")
        if item.price_efhc is None:
            return (False, "price_not_set")
        if total_efhc < item.price_efhc:
            return (False, "insufficient_efhc_balance")
        return (True, "")

    def method_supported(item: ShopItemDTO, method: str) -> bool:
        return method in item.pay_currencies

    out_items: List[Dict[str, Any]] = []
    for it in items:
        can_efhc, reason_efhc = check_efhc_afford(it)
        efhc_enabled = can_efhc and method_supported(it, PAY_EFHC)
        ton_enabled = method_supported(it, PAY_TON) and (it.price_ton is not None)
        usdt_enabled = method_supported(it, PAY_USDT) and (it.price_usdt is not None)

        out_items.append({
            "id": it.id,
            "code": it.code,
            "title": it.title,
            "type": it.item_type,
            "prices": {
                "EFHC": (str(it.price_efhc) if it.price_efhc is not None else None),
                "TON": (str(it.price_ton) if it.price_ton is not None else None),
                "USDT": (str(it.price_usdt) if it.price_usdt is not None else None),
            },
            "supported_methods": it.pay_currencies,
            "available": {
                "EFHC": {
                    "enabled": efhc_enabled,
                    "disabled_reason": None if efhc_enabled else (
                        reason_efhc if not can_efhc else "method_not_supported"
                    ),
                },
                "TON": {
                    "enabled": ton_enabled,
                    "disabled_reason": None if ton_enabled else (
                        "price_not_set" if it.price_ton is None else "method_not_supported"
                    ),
                },
                "USDT": {
                    "enabled": usdt_enabled,
                    "disabled_reason": None if usdt_enabled else (
                        "price_not_set" if it.price_usdt is None else "method_not_supported"
                    ),
                },
            },
            "extra": it.extra_json,
        })

    return {
        "balances": {
            "main_balance": str(main),
            "bonus_balance": str(bonus),
            "total_efhc": str(total_efhc),
        },
        "items": out_items,
    }

# ---- MEMO-формат по канону ----------------------------------------------------

def _build_memo_for_external(item: ShopItemDTO, user_tg_id: int) -> str:
    """
    EFHC-пакеты: SKU:EFHC|Q:<INT>|TG:<id>
    VIP NFT:    SKU:NFT_VIP|Q:1|TG:<id>
    Прочие SKU можно расширять по мере добавления типов товаров.
    """
    if item.item_type == ITEM_TYPE_EFHC_PACKAGE:
        qty = 0
        if item.extra_json and "quantity_efhc" in item.extra_json:
            try:
                qty = int(item.extra_json["quantity_efhc"])
            except Exception:
                qty = 0
        return f"SKU:EFHC|Q:{qty}|TG:{user_tg_id}"
    elif item.item_type == ITEM_TYPE_NFT_VIP:
        return f"SKU:NFT_VIP|Q:1|TG:{user_tg_id}"
    return f"SKU:UNKNOWN|TG:{user_tg_id}"

# ---- Загрузка карточки товара по code -----------------------------------------

async def _get_item_by_code(db: AsyncSession, code: str) -> ShopItemDTO:
    q = text(f"""
        SELECT id, code, title, item_type, is_active,
               price_efhc, price_ton, price_usdt, pay_currencies, extra_json
        FROM {SCHEMA}.shop_items
        WHERE code = :code AND COALESCE(is_active, TRUE) = TRUE
        LIMIT 1
    """)
    r = (await db.execute(q, {"code": code})).mappings().first()
    if not r:
        raise ValueError(f"shop_item code={code} not found or inactive")
    return ShopItemDTO(
        id=r["id"],
        code=r["code"],
        title=r["title"],
        item_type=r["item_type"],
        is_active=bool(r["is_active"]),
        price_efhc=(d8(r["price_efhc"]) if r["price_efhc"] is not None else None),
        price_ton=(d8(r["price_ton"]) if r["price_ton"] is not None else None),
        price_usdt=(d8(r["price_usdt"]) if r["price_usdt"] is not None else None),
        pay_currencies=_pay_methods_from_row(r),
        extra_json=r["extra_json"] or {},
    )

# ---- Создание заказа: внешняя оплата (TON/USDT) -------------------------------

async def create_order_external(
    db: AsyncSession,
    *,
    user_id: int,
    user_tg_id: int,
    item_code: str,
    payment_method: str,
    client_order_id: str,
) -> Dict[str, Any]:
    """
    Создаёт заказ под внешнюю оплату (TON/USDT) для выбранной карточки товара, генерирует MEMO.
    Денежных списаний здесь НЕТ — деньги придут извне, вотчер подтвердит и начислит/создаст заявку.

    Идемпотентность:
      • client_order_id ОБЯЗАТЕЛЕН и должен быть уникален на стороне клиента.
      • Повторный вызов с тем же client_order_id вернёт уже существующий заказ (read-through).
    """
    if not client_order_id or not client_order_id.strip():
        raise ValueError("client_order_id is required for external orders (idempotency)")

    if payment_method not in (PAY_TON, PAY_USDT):
        raise ValueError("external payments support only TON/USDT")

    item = await _get_item_by_code(db, item_code)
    if payment_method == PAY_TON and item.price_ton is None:
        raise ValueError("price_ton not set for item")
    if payment_method == PAY_USDT and item.price_usdt is None:
        raise ValueError("price_usdt not set for item")
    if payment_method not in item.pay_currencies:
        raise ValueError("payment method not supported for this item")

    memo = _build_memo_for_external(item, user_tg_id=user_tg_id)

    # Идемпотентно создаём PENDING-заказ
    q = text(f"""
        WITH ins AS (
          INSERT INTO {SCHEMA}.shop_orders
            (user_id, item_id, status, payment_method, memo, client_order_id, created_at, updated_at)
          VALUES (:uid, :item_id, :status, :pm, :memo, :coid, NOW(), NOW())
          ON CONFLICT (client_order_id) DO NOTHING
          RETURNING id, user_id, item_id, status, payment_method, memo, tx_hash, created_at
        )
        SELECT * FROM ins
        UNION ALL
        SELECT id, user_id, item_id, status, payment_method, memo, tx_hash, created_at
        FROM {SCHEMA}.shop_orders
        WHERE client_order_id = :coid
        LIMIT 1
    """)
    row = (await db.execute(q, {
        "uid": user_id,
        "item_id": item.id,
        "status": ORDER_STATUS_PENDING,
        "pm": payment_method,
        "memo": memo,
        "coid": client_order_id,
    })).mappings().first()
    await db.commit()

    order = OrderDTO(
        id=row["id"], user_id=row["user_id"], item_id=row["item_id"],
        status=row["status"], payment_method=row["payment_method"],
        memo=row["memo"], tx_hash=row["tx_hash"], created_at=row["created_at"]
    )
    return {
        "order": asdict(order),
        "pay": {
            "method": payment_method,
            # Цену берём из карточки товара — UI сам отобразит стоимость стороне TON/USDT.
            "amount": str(item.price_ton if payment_method == PAY_TON else item.price_usdt),
            "memo": memo,
            "to_wallet": getattr(settings, "TON_MAIN_WALLET", None),  # адрес проекта
        }
    }

# ---- Создание заказа: внутренняя оплата EFHC ----------------------------------

async def purchase_with_efhc(
    db: AsyncSession,
    *,
    user_id: int,
    item_code: str,
    idempotency_key: str,
) -> Dict[str, Any]:
    """
    Покупка товара за EFHC (внутри бота): списание bonus→main с зачислением в Банк.

    Ограничения:
      • EFHC-пакеты нельзя покупать EFHC (запрещено).
      • Пользователь не должен уходить в минус: если не хватает — ошибка.
      • Для VIP NFT: после списания EFHC создаётся заказ со статусом PAID_PENDING_MANUAL
        (ручная выдача админом), вотчер здесь не участвует.
      • Для прочих виртуальных товаров: заказ помечается PAID_AUTO (автодоставка логическая).

    Идемпотентность:
      • строго через idempotency_key;
      • денежные операции используют под-ключи idk (":B", ":M"), заказ — client_order_id=idk.
    """
    if not idempotency_key or not idempotency_key.strip():
        raise ValueError("Idempotency-Key is required")

    item = await _get_item_by_code(db, item_code)
    if PAY_EFHC not in item.pay_currencies:
        raise ValueError("EFHC payment not supported for this item")
    if item.price_efhc is None:
        raise ValueError("price_efhc not set")

    # 1) Проверка балансов пользователя (минус запрещён)
    main, bonus = await _get_user_balances(db, user_id)
    need = d8(item.price_efhc)
    if d8(main + bonus) < need:
        raise ValueError("insufficient_efhc_balance")

    # 2) Списание: сначала бонусы, затем основной баланс. Все списания — в Банк.
    #    Идемпотентность: делим ключ на под-операции, чтобы повтор был безопасен.
    spend_bonus = min(bonus, need)
    spend_main = d8(need - spend_bonus)

    if spend_bonus > 0:
        await bank.debit_user_bonus_to_bank(
            db,
            user_id=user_id,
            amount=spend_bonus,
            reason=f"shop:{item.code}",
            idempotency_key=f"{idempotency_key}:B",
        )
    if spend_main > 0:
        await bank.debit_user_to_bank(
            db,
            user_id=user_id,
            amount=spend_main,
            reason=f"shop:{item.code}",
            idempotency_key=f"{idempotency_key}:M",
        )

    # 3) Создаём заказ в нужном статусе
    status = ORDER_STATUS_PAID_AUTO
    if item.item_type == ITEM_TYPE_NFT_VIP:
        status = ORDER_STATUS_PAID_PENDING_MANUAL  # ручная выдача VIP (заявка)

    q = text(f"""
        INSERT INTO {SCHEMA}.shop_orders
          (user_id, item_id, status, payment_method, memo, client_order_id, created_at, updated_at)
        VALUES (:uid, :item_id, :status, :pm, NULL, :coid, NOW(), NOW())
        ON CONFLICT (client_order_id) DO NOTHING
        RETURNING id, user_id, item_id, status, payment_method, memo, tx_hash, created_at
    """)
    row = (await db.execute(q, {
        "uid": user_id,
        "item_id": item.id,
        "status": status,
        "pm": PAY_EFHC,
        "coid": idempotency_key,  # используем idk как client_order_id для идемпотентности
    })).mappings().first()

    if not row:
        # read-through: вернуть уже существующий заказ по client_order_id
        r2 = (await db.execute(text(f"""
            SELECT id, user_id, item_id, status, payment_method, memo, tx_hash, created_at
            FROM {SCHEMA}.shop_orders
            WHERE client_order_id = :coid
            LIMIT 1
        """), {"coid": idempotency_key})).mappings().first()
        if not r2:
            await db.rollback()
            raise RuntimeError("idempotent order insert failed unexpectedly")
        row = r2

    await db.commit()

    order = OrderDTO(
        id=row["id"], user_id=row["user_id"], item_id=row["item_id"],
        status=row["status"], payment_method=row["payment_method"],
        memo=row["memo"], tx_hash=row["tx_hash"], created_at=row["created_at"]
    )
    return {"order": asdict(order)}

# ---- Админ-APIs для управления ценами -----------------------------------------

async def admin_set_price(
    db: AsyncSession,
    *,
    code: str,
    price_efhc: Optional[Decimal] = None,
    price_ton: Optional[Decimal] = None,
    price_usdt: Optional[Decimal] = None,
    pay_currencies: Optional[List[str]] = None,
    is_active: Optional[bool] = None,
) -> Dict[str, Any]:
    """
    Из админ-панели: задать/обновить цены и доступные методы оплаты карточки товара.
    Все цены квантуются до 8 знаков (d8). Можно включать/выключать товар.
    """
    pe = d8(price_efhc) if price_efhc is not None else None
    pt = d8(price_ton) if price_ton is not None else None
    pu = d8(price_usdt) if price_usdt is not None else None

    sets = ["updated_at = NOW()"]
    params: Dict[str, Any] = {"code": code}

    if pe is not None:
        sets.append("price_efhc = :pe")
        params["pe"] = pe
    if pt is not None:
        sets.append("price_ton = :pt")
        params["pt"] = pt
    if pu is not None:
        sets.append("price_usdt = :pu")
        params["pu"] = pu
    if pay_currencies is not None:
        sets.append("pay_currencies = :pays")
        params["pays"] = pay_currencies
    if is_active is not None:
        sets.append("is_active = :ia")
        params["ia"] = bool(is_active)

    if len(sets) == 1:
        return {"updated": 0}

    q = text(f"""
        UPDATE {SCHEMA}.shop_items
        SET {", ".join(sets)}
        WHERE code = :code
        RETURNING id
    """)
    r = (await db.execute(q, params)).first()
    await db.commit()
    return {"updated": 1 if r else 0}

# ---- Витрина заказов пользователя (для WebApp) --------------------------------

async def list_orders_for_user(
    db: AsyncSession,
    *,
    user_id: int,
    limit: int = 50,
    after_id: Optional[int] = None,
) -> List[OrderDTO]:
    """
    Список заказов пользователя (курсорно по id DESC).
    Роутер добавит обёртку с ETag и next_cursor.
    """
    if limit > 200:
        limit = 200
    cond = "user_id = :uid"
    params: Dict[str, Any] = {"uid": user_id}
    if after_id:
        cond += " AND id < :after_id"
        params["after_id"] = after_id

    q = text(f"""
        SELECT id, user_id, item_id, status, payment_method, memo, tx_hash, created_at
        FROM {SCHEMA}.shop_orders
        WHERE {cond}
        ORDER BY id DESC
        LIMIT :lim
    """)
    params["lim"] = limit
    rows = (await db.execute(q, params)).mappings().all()
    out: List[OrderDTO] = []
    for r in rows:
        out.append(OrderDTO(
            id=r["id"], user_id=r["user_id"], item_id=r["item_id"],
            status=r["status"], payment_method=r["payment_method"],
            memo=r["memo"], tx_hash=r["tx_hash"], created_at=r["created_at"]
        ))
    return out

# ---- Админ-витрина заказов (глобальный список с курсором) ---------------------

async def admin_list_orders(
    db: AsyncSession,
    *,
    limit: int = 50,
    cursor: Optional[str] = None,
    status: Optional[str] = None,
    item_type: Optional[str] = None,
    user_id: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Админская витрина заказов с курсорной пагинацией.
    Сортировка: created_at DESC, id DESC.
    Курсор кодирует (created_at_iso, id) через encode_cursor/decode_cursor.

    Фильтры:
      • status    — ORDER_STATUS_*
      • item_type — ITEM_TYPE_EFHC_PACKAGE / ITEM_TYPE_NFT_VIP / ITEM_TYPE_VIRTUAL
      • user_id   — конкретный пользователь.
    """
    limit = max(1, min(int(limit or 50), 200))

    after_created_at: Optional[str] = None
    after_id: Optional[int] = None
    if cursor:
        try:
            after_created_at, after_id = decode_cursor(cursor)
        except Exception as e:
            logger.warning("admin_list_orders: bad cursor %s: %s", cursor, e)

    conds = ["1=1"]
    params: Dict[str, Any] = {}

    if status:
        conds.append("o.status = :st")
        params["st"] = status
    if user_id:
        conds.append("o.user_id = :uid")
        params["uid"] = user_id
    if item_type:
        conds.append("i.item_type = :itype")
        params["itype"] = item_type
    if after_created_at and after_id is not None:
        conds.append("(o.created_at, o.id) < (:ca, :cid)")
        params["ca"] = after_created_at
        params["cid"] = after_id

    where = " AND ".join(conds)

    q = text(f"""
        SELECT
          o.id,
          o.user_id,
          u.username,
          o.item_id,
          i.code AS item_code,
          i.title AS item_title,
          i.item_type,
          o.status,
          o.payment_method,
          o.memo,
          o.tx_hash,
          o.created_at
        FROM {SCHEMA}.shop_orders o
        LEFT JOIN {SCHEMA}.shop_items i ON i.id = o.item_id
        LEFT JOIN {SCHEMA}.users u ON u.id = o.user_id
        WHERE {where}
        ORDER BY o.created_at DESC, o.id DESC
        LIMIT :lim
    """)
    params["lim"] = limit + 1  # на один больше для определения наличия next_cursor

    rs = await db.execute(q, params)
    rows = rs.fetchall()

    items: List[Dict[str, Any]] = []
    for r in rows[:limit]:
        items.append({
            "id": int(r[0]),
            "user_id": int(r[1]),
            "username": r[2],
            "item_id": int(r[3]) if r[3] is not None else None,
            "item_code": r[4],
            "item_title": r[5],
            "item_type": r[6],
            "status": r[7],
            "payment_method": r[8],
            "memo": r[9],
            "tx_hash": r[10],
            "created_at": r[11].isoformat() if r[11] else None,
        })

    next_cursor: Optional[str] = None
    if len(rows) > limit:
        last = rows[limit - 1]
        last_created_at = last[11]
        last_id = last[0]
        if last_created_at:
            next_cursor = encode_cursor(last_created_at.isoformat(), int(last_id))

    return {"items": items, "next_cursor": next_cursor}

# =============================================================================
# Пояснения «для чайника»:
# • Этот модуль не рассылает деньги сам — все денежные движения проводит
#   банковский сервис (transactions_service); здесь мы только инициируем списания EFHC.
# • EFHC-пакеты оплачиваются только внешне (TON/USDT). Мы создаём заказ PENDING
#   и генерируем MEMO. Подтверждение оплаты делает вотчер по tx_hash, затем:
#     – EFHC-пакеты: кредит EFHC пользователю из Банка (read-through идемпотентность).
#     – VIP NFT: создаётся заявка PAID_PENDING_MANUAL (ручная выдача админом).
# • Внутренние EFHC-покупки (виртуальные товары, VIP за EFHC):
#     – списание bonus→main с зачислением в Банк (две операции с под-ключами idk).
#     – заявка на VIP создаётся как PAID_PENDING_MANUAL (без автодоставки).
# • Безопасность/ИИ:
#     – Идемпотентность: все денежные действия используют idempotency_key (или client_order_id).
#     – Витрина: возвращает флаги доступности методов оплаты и причины отключения кнопок.
#     – Запрет минуса у пользователей: если EFHC не хватает — покупка за EFHC не выполняется.
# • Админ-витрина:
#     – admin_list_orders даёт глобальный список заказов с фильтрами и курсором по (created_at,id),
#       чтобы админка могла безопасно листать историю без OFFSET.
# =============================================================================
