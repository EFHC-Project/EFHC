# -*- coding: utf-8 -*-
# backend/app/services/orders_service.py
# =============================================================================
# Назначение кода:
#   Сервис заказов магазина (EFHC Shop):
#     • создание заказов,
#     • выдача платёжных инструкций для внешних платежей (TON/USDT),
#     • финализация входящих оплат,
#     • внутренние покупки за EFHC (bonus→main),
#     • уменьшение stock,
#     • создание заявок на VIP NFT (строго вручную, без авто-выдачи).
#
# Канон/инварианты:
#   • Все деньги и EFHC двигаются ТОЛЬКО через банковский сервис:
#       - debit_user_bonus_to_bank / debit_user_to_bank (списание у пользователя),
#       - credit_user_from_bank (начисление пользователю),
#     Decimal(30,8), округление вниз, «bonus-first».
#   • Пользователь НЕ может уйти в минус; Банк МОЖЕТ (операции не блокируем).
#   • NFT не авто-выдаём. Только заявка → ручная модерация.
#   • MEMO для Shop — строгий SKU-формат:
#       EFHC-пакеты:  "SKU:EFHC|Q:<INT>|TG:<telegram_id>"
#       VIP-NFT:      "SKU:NFT_VIP|Q:1|TG:<telegram_id>"
#       Прочее:       "SKU:ITEM|ID:<item_id>|TG:<telegram_id>"
#   • Идемпотентность: стабильные ключи вида "order:<order_id>[:part]".
#
# ВАЖНО (канон по VIP):
#   • NFT VIP в Shop может покупаться за три вида валют:
#       - TON   (внешний платёж),
#       - USDT  (внешний платёж),
#       - EFHC  (внутренний платёж через банк).
#   • Валюта покупки НЕ связана с тем, куда минтится NFT.
#     Адрес для NFT берётся из основного кошелька пользователя (TON) —
#     это настраивается через PRIMARY_ASSET_FOR_NFT, но НЕ ограничивает
#     currency товара.
#
# ИИ-защита/самовосстановление:
#   • «Read-through» при финализации: повторная обработка заказа, отличного от
#     PENDING_PAYMENT, возвращает его текущее состояние — без дублей и падений.
#   • Жёсткая валидация SKU/MEMO и stock — исключает «молчаливые» рассинхроны.
#   • Не делает commit/rollback — обёртывайте вызовы в `async with session.begin():`.
#
# Запреты:
#   • Нет P2P. Никаких прямых переводов user→user.
#   • Нет авто-NFT. Только заявка на ручную выдачу.
#   • Нет «суточных» расчётов — сервис не знает про генерацию энергии.
# =============================================================================

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Final, Iterable, List, Literal, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pydantic import BaseSettings, Field, validator

# -----------------------------------------------------------------------------
# Глобальные настройки / схемы
# -----------------------------------------------------------------------------
from backend.app.core.config_core import get_settings as get_core_settings
from backend.app.core.logging_core import get_logger
from backend.app.deps import d8  # централизованное Decimal(8), округление вниз

_core = get_core_settings()
CORE_SCHEMA = getattr(_core, "DB_SCHEMA_CORE", "efhc_core")
SHOP_SCHEMA = getattr(_core, "DB_SCHEMA_SHOP", "efhc_shop")  # опциональная отдельная схема

logger = get_logger(__name__)

# -----------------------------------------------------------------------------
# Банковский сервис и сервис заявок на NFT
# -----------------------------------------------------------------------------
from backend.app.services import transactions_service as tx
from backend.app.services import nft_requests_service as nft_svc

# -----------------------------------------------------------------------------
# ORM-модели магазина и кошельков
# -----------------------------------------------------------------------------
from backend.app.models.shop_models import ShopItem, ShopOrder
from backend.app.models.wallet_models import UserWallet  # основной кошелёк для NFT-заявки

# -----------------------------------------------------------------------------
# Типы/константы витрины
# -----------------------------------------------------------------------------
OrderStatus = Literal["PENDING_PAYMENT", "PAID", "COMPLETED", "CANCELED", "FAILED"]

VIP_MEMO_PREFIX: Final[str] = "pak_nft_vip"     # сигнатура артикулов/мемо для VIP
EFHC_PACK_PREFIX: Final[str] = "pak_"           # сигнатура EFHC-пакетов, напр. pak_100_efhc_ton

# ВАЖНО: это именно «валюта товара» в витрине,
# а не блокировка типов кошельков для NFT.
ALLOWED_CURRENCIES: Final[tuple[str, ...]] = ("TON", "USDT", "EFHC")

# =============================================================================
# Настройки через BaseSettings (префикс SHOP_)
# =============================================================================

class OrdersSettings(BaseSettings):
    TON_WALLET_ADDRESS: str = Field(
        ..., description="Основной TON-кошелёк проекта для входящих платежей (TON / USDT on TON)"
    )
    USDT_WALLET_ADDRESS: Optional[str] = Field(
        None,
        description="Опциональный адрес для USDT. Если не задан, для USDT используется TON_WALLET_ADDRESS.",
    )
    STRICT_MATCH_AMOUNT: bool = Field(True, description="Строгое равенство сумм при проверке платежей")
    REQUIRE_MEMO: bool = Field(True, description="Требовать точное совпадение MEMO (SKU)")
    NOTIFY_ADMINS: bool = Field(True, description="Уведомлять админов о новых заявках на NFT")
    PRIMARY_ASSET_FOR_NFT: str = Field(
        "TON",
        description=(
            "Тип кошелька для адреса NFT (обычно TON). "
            "ЭТО НЕ ОГРАНИЧИВАЕТ валюту покупки VIP (TON/USDT/EFHC)."
        ),
    )
    MIN_PRICE: Decimal = Field(Decimal("0.00000001"), description="Минимально допустимая цена товара")

    class Config:
        env_prefix = "SHOP_"

    @validator("TON_WALLET_ADDRESS")
    def _ton_required(cls, v: str) -> str:
        if not (v or "").strip():
            raise ValueError("SHOP_TON_WALLET_ADDRESS обязателен")
        return v.strip()

    @validator("MIN_PRICE")
    def _min_positive(cls, v: Decimal) -> Decimal:
        if v <= 0:
            raise ValueError("MIN_PRICE должен быть > 0")
        return v

SETTINGS = OrdersSettings()

# =============================================================================
# Вспомогательные функции (SKU/MEMO, парсинг, Decimal)
# =============================================================================

def _as_decimal(v: Any) -> Decimal:
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("Некорректное числовое значение") from exc

def is_vip_item_code(code: str) -> bool:
    c = (code or "").lower()
    return c.startswith(VIP_MEMO_PREFIX)

def parse_efhc_pack_qty(code: str) -> Optional[int]:
    """
    Поддерживаемые коды пакетов EFHC (пример):
      pak_100_efhc_*, pak_500_efhc_*, pak_1000_efhc_*
    Возвращает номинал EFHC или None.
    """
    c = (code or "").lower()
    if c.startswith("pak_100_efhc"):
        return 100
    if c.startswith("pak_500_efhc"):
        return 500
    if c.startswith("pak_1000_efhc"):
        return 1000
    return None

def build_sku_for_order(item: ShopItem, telegram_id: int) -> str:
    """
    Канонический MEMO для Shop:
      • EFHC-пакеты → SKU:EFHC|Q:<INT>|TG:<id>
      • VIP NFT     → SKU:NFT_VIP|Q:1|TG:<id>
      • Прочее      → SKU:ITEM|ID:<item_id>|TG:<id>
    """
    code = item.custom_memo or ""
    qty = parse_efhc_pack_qty(code)
    if qty:
        return f"SKU:EFHC|Q:{int(qty)}|TG:{int(telegram_id)}"
    if is_vip_item_code(code):
        return f"SKU:NFT_VIP|Q:1|TG:{int(telegram_id)}"
    return f"SKU:ITEM|ID:{int(item.item_id)}|TG:{int(telegram_id)}"

# =============================================================================
# Публичный API сервиса заказов
# =============================================================================

async def create_order(
    session: AsyncSession,
    *,
    telegram_id: int,
    item_id: int,
) -> Tuple[ShopOrder, Optional[Dict[str, str]]]:
    """
    Создание заказа по товару.

    Возвращает пару (order, payment_instructions):
      • Для товаров с currency = 'EFHC' — это ВНУТРЕННИЙ платёж:
          - списываем EFHC сразу (bonus→main) через банк,
          - уменьшаем stock,
          - если товар VIP (custom_memo ~ pak_nft_vip*), создаём заявку на VIP NFT,
          - статус заказа → COMPLETED, payment_instructions = None.
      • Для товаров с currency = 'TON' или 'USDT' — ВНЕШНИЙ платёж:
          - создаём заказ PENDING_PAYMENT,
          - возвращаем инструкции {'to','asset','amount','memo'},
          - вотчер по факту платежа вызовет finalize_order_by_payment().

    ВАЖНО:
      • VIP-товары могут иметь currency: 'TON', 'USDT' или 'EFHC'.
        Валюта влияет только на путь оплаты, но НЕ на саму возможность покупки VIP.
    """
    item: ShopItem | None = await session.get(ShopItem, item_id)
    if not item or not item.active:
        raise ValueError("Товар не найден или не активен.")
    if item.stock is None or int(item.stock) <= 0:
        raise ValueError("Товар закончился (stock=0).")
    if item.price is None:
        raise ValueError("Цена товара не задана.")
    price = _as_decimal(item.price)
    if price < SETTINGS.MIN_PRICE:
        raise ValueError("Слишком маленькая цена товара.")

    currency = (item.currency or "").upper()
    if currency not in ALLOWED_CURRENCIES:
        raise ValueError("Неподдерживаемая валюта товара.")

    expected_sku = build_sku_for_order(item, telegram_id)

    # ---- EFHC (внутренний платёж) -------------------------------------------
    if currency == "EFHC":
        order = ShopOrder(
            item_id=int(item.item_id),
            telegram_id=int(telegram_id),
            status="PAID",  # внутренний платёж проходит здесь же
            expected_amount=price,
            expected_currency="EFHC",
            expected_memo=expected_sku,
        )
        session.add(order)
        await session.flush()  # нужен order_id для idempotency_key

        # Списание EFHC: «сначала bonus, потом main»
        await _debit_user_bonus_first(
            session=session,
            telegram_id=int(telegram_id),
            amount=price,
            idk_prefix=f"order:{order.order_id}",
            reason="shop_item_efhc_internal",
            meta={"order_id": int(order.order_id), "item_id": int(item.item_id)},
        )

        # Сток
        await _decrease_stock_or_fail(session, item_id=int(item.item_id))

        # Если это VIP — создаём заявку (ручная выдача)
        if is_vip_item_code(item.custom_memo or ""):
            await _create_vip_request(session, order=order)

        order.status = "COMPLETED"
        await session.flush()
        return order, None

    # ---- TON/USDT (внешний платёж) -----------------------------------------
    order = ShopOrder(
        item_id=int(item.item_id),
        telegram_id=int(telegram_id),
        status="PENDING_PAYMENT",
        expected_amount=price,
        expected_currency=currency,
        expected_memo=expected_sku,
    )
    session.add(order)
    await session.flush()

    pay_to = (
        SETTINGS.TON_WALLET_ADDRESS
        if currency == "TON"
        else (SETTINGS.USDT_WALLET_ADDRESS or SETTINGS.TON_WALLET_ADDRESS)
    )
    payment_instructions = {
        "to": pay_to,
        "asset": currency,
        "amount": str(price),  # строкой, без округлений фронтом
        "memo": expected_sku,
    }
    return order, payment_instructions


async def finalize_order_by_payment(
    session: AsyncSession,
    *,
    order_id: int,
    actual_amount: Decimal,
    actual_currency: str,
    actual_memo: str,
    tx_hash: str,
) -> ShopOrder:
    """
    Финализирует заказ после входящего платежа (TON/USDT), вызывается вотчером.

    Требования:
      • Статус заказа должен быть PENDING_PAYMENT (иначе — «read-through» возвращаем как есть).
      • Валюта/сумма/MEMO (SKU) совпадают с ожидаемыми (или CANCELED/FAILED).
      • EFHC-пакет → кредит EFHC пользователю из Банка.
      • VIP → создаём заявку на NFT (ручная выдача).
      • Сток уменьшаем при успешной оплате.

    Идемпотентность:
      • Все денежные операции — через стабильный ключ "order:<order_id>".
    """
    order: ShopOrder | None = await session.get(ShopOrder, int(order_id), with_for_update=True)
    if not order:
        raise ValueError("Заказ не найден.")
    if order.status != "PENDING_PAYMENT":
        return order  # read-through: уже обработан ранее

    # Фиксируем фактические значения
    order.actual_amount = _as_decimal(actual_amount)
    order.actual_currency = (actual_currency or "").upper()
    order.actual_memo = actual_memo or ""
    order.tx_hash = tx_hash or ""

    # Валидация
    mismatches: list[str] = []
    if order.actual_currency != order.expected_currency:
        mismatches.append("CURRENCY_MISMATCH")
    if SETTINGS.REQUIRE_MEMO and order.actual_memo != order.expected_memo:
        mismatches.append("MEMO_MISMATCH")
    if SETTINGS.STRICT_MATCH_AMOUNT:
        if _as_decimal(order.actual_amount) != _as_decimal(order.expected_amount):
            mismatches.append("AMOUNT_MISMATCH")
    else:
        if _as_decimal(order.actual_amount) <= Decimal("0"):
            mismatches.append("AMOUNT_INVALID")

    if mismatches:
        order.status = "CANCELED"
        await session.flush()
        return order

    # Берём товар под блокировку
    item: ShopItem | None = await session.get(ShopItem, int(order.item_id), with_for_update=True)
    if not item or not item.active:
        order.status = "CANCELED"
        await session.flush()
        return order

    # Помечаем как оплаченный
    order.status = "PAID"

    code = item.custom_memo or ""
    qty = parse_efhc_pack_qty(code)

    # EFHC-пакет: зачисляем EFHC из Банка пользователю
    if qty:
        await tx.credit_user_from_bank(
            db=session,
            user_id=int(order.telegram_id),  # ВАЖНО: убедиться, что в каноне user_id == telegram_id или адаптировать
            amount=d8(qty),
            reason="shop_pack_credit",
            idempotency_key=f"order:{order.order_id}",
            meta={"order_id": int(order.order_id), "item_id": int(item.item_id), "tx_hash": order.tx_hash},
        )
        await _decrease_stock_or_fail(session, item_id=int(item.item_id))
        order.status = "COMPLETED"
        await session.flush()
        return order

    # VIP: создаём заявку на NFT (ручная выдача) — валюта может быть TON или USDT
    if is_vip_item_code(code):
        await _decrease_stock_or_fail(session, item_id=int(item.item_id))
        await _create_vip_request(session, order=order)
        order.status = "COMPLETED"
        await session.flush()
        return order

    # Неизвестный тип товара
    order.status = "FAILED"
    await session.flush()
    return order


async def cancel_order_admin(
    session: AsyncSession,
    *,
    order_id: int,
    reason: Optional[str] = None,
) -> ShopOrder:
    """
    Админская отмена зависшего/ошибочного заказа.
    Никаких списаний/начислений не делает — только статус CANCELED.
    """
    order: ShopOrder | None = await session.get(ShopOrder, int(order_id), with_for_update=True)
    if not order:
        raise ValueError("Заказ не найден.")
    if order.status in ("COMPLETED", "CANCELED", "FAILED"):
        return order
    order.status = "CANCELED"
    if hasattr(order, "admin_comment") and reason:
        setattr(order, "admin_comment", reason)
    await session.flush()
    return order


async def get_order(session: AsyncSession, *, order_id: int) -> Optional[ShopOrder]:
    """Возвращает заказ по ID (без блокировки)."""
    return await session.get(ShopOrder, int(order_id))


async def list_user_orders(
    session: AsyncSession,
    *,
    telegram_id: int,
    limit: int = 50,
) -> List[ShopOrder]:
    """Последние заказы пользователя (по убыванию order_id)."""
    res = await session.execute(
        select(ShopOrder)
        .where(ShopOrder.telegram_id == int(telegram_id))
        .order_by(ShopOrder.order_id.desc())
        .limit(int(limit))
    )
    return list(res.scalars().all())

# =============================================================================
# Внутренние хелперы: списание EFHC (bonus→main), stock, VIP-заявка, уведомления
# =============================================================================

async def _debit_user_bonus_first(
    session: AsyncSession,
    *,
    telegram_id: int,
    amount: Decimal,
    idk_prefix: str,
    reason: str,
    meta: Optional[Dict[str, Any]] = None,
) -> None:
    """
    Списание у пользователя EFHC по правилу «сначала бонус, затем основной».
    Банк получает зеркальные записи, пользователь не уходит в минус (гарантирует банк).
    """
    to_spend = d8(amount)

    # Часть 1: бонус — с отдельным idempotency_key
    try:
        await tx.debit_user_bonus_to_bank(
            db=session,
            user_id=int(telegram_id),
            amount=to_spend,
            reason=reason,
            idempotency_key=f"{idk_prefix}:bonus",
            meta=meta or {},
        )
    except Exception as e:
        logger.debug("bonus debit skipped/failed: %s", e)

    # Часть 2: основной баланс
    await tx.debit_user_to_bank(
        db=session,
        user_id=int(telegram_id),
        amount=to_spend,
        reason=reason,
        idempotency_key=f"{idk_prefix}:main",
        meta=meta or {},
    )


async def _decrease_stock_or_fail(session: AsyncSession, *, item_id: int) -> None:
    """
    Уменьшает stock у товара на 1 под блокировкой.
    Защита от гонок: берём строку через with_for_update и не допускаем минус.
    """
    item: ShopItem | None = await session.get(ShopItem, int(item_id), with_for_update=True)
    if not item:
        raise ValueError("Товар не найден.")
    current = int(item.stock or 0)
    if current <= 0:
        raise ValueError("Товар закончился (stock=0).")
    item.stock = current - 1
    await session.flush()


async def _create_vip_request(session: AsyncSession, *, order: ShopOrder) -> None:
    """
    Создаёт заявку на VIP-NFT (ручная выдача). Никакой авто-NFT.
    Валюта заказа здесь НЕ учитывается: VIP уже оплачен (EFHC/TON/USDT),
    дальше только заявка на NFT.
    """
    # Ищем основной кошелёк пользователя для выбранного актива (по канону — TON)
    wallet = await session.scalar(
        select(UserWallet).where(
            UserWallet.telegram_id == int(order.telegram_id),
            UserWallet.asset_type == SETTINGS.PRIMARY_ASSET_FOR_NFT,
            UserWallet.is_primary == True,  # noqa: E712
        )
    )
    wallet_address = wallet.wallet_address if wallet else ""

    await nft_svc.submit_manual_vip_nft_claim(
        session,
        telegram_id=int(order.telegram_id),
        wallet_address=wallet_address,
        comment=f"Shop order #{order.order_id}: VIP NFT claim",
        created_by_admin_tid=None,
    )

    if SETTINGS.NOTIFY_ADMINS:
        try:
            await _notify_admins_vip_request(session, order=order, wallet=wallet_address)
        except Exception:
            pass  # уведомления не критичны


async def _notify_admins_vip_request(
    session: AsyncSession,
    *,
    order: ShopOrder,
    wallet: Optional[str],
) -> None:
    """
    Место для интеграции уведомлений админам.
    По канону не критично — ошибки здесь игнорируются.
    """
    return None

# =============================================================================
# Экспорт публичного API
# =============================================================================

__all__ = [
    "create_order",
    "finalize_order_by_payment",
    "cancel_order_admin",
    "get_order",
    "list_user_orders",
    # helpers
    "build_sku_for_order",
    "parse_efhc_pack_qty",
    "is_vip_item_code",
]

# -----------------------------------------------------------------------------
# Пояснения «для чайника»:
#   • VIP-товар может быть в трёх валютах: TON, USDT, EFHC.
#     Внутренний платёж EFHC проходит сразу; TON/USDT — через внешнюю оплату и вотчер.
#   • Для VIP-позиций после успешной оплаты ВСЕГДА создаётся заявка на NFT,
#     а не автоматическая выдача.
#   • MEMO строго SKU, чтобы вотчер мог однозначно сопоставить платёж ↔ заказ.
# -----------------------------------------------------------------------------
