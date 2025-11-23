# -*- coding: utf-8 -*-
# backend/app/services/watcher_service.py
# =============================================================================
# EFHC Bot — Вотчер входящих платежей TON (автозачисления через Банк)
# -----------------------------------------------------------------------------
# Назначение:
#   Обрабатывает входящие транзакции TON по адресу проекта, разбирает MEMO по
#   канону и выполняет соответствующие действия: автодоставка EFHC-пакетов,
#   создание заявки на NFT (ручная выдача), автоматический депозит EFHC по
#   правилам. Все денежные движения — строго через банковский сервис.
#
# Канон/инварианты:
#   • Только посекундная генерация в проекте (здесь не применяется, но важно).
#   • Денежные операции производит единый банк: никакой «эмиссии в обход».
#   • P2P запрещён. Обратной конверсии EFHC→kWh нет.
#   • NFT: только заявка (PAID_PENDING_MANUAL), никакой автодоставки.
#   • Идемпотентность по tx_hash (UNIQUE в ton_inbox_logs) + read-through.
#   • Идентификация пользователя:
#       1) по привязанному активному кошельку (user_wallets),
#       2) при отсутствии — по telegram_id из MEMO.
#
# ИИ-защиты / самовосстановление:
#   • Read-through: повторная обработка одной и той же транзакции не даёт дублей
#     (UNIQUE(tx_hash) + возврат финального результата).
#   • «Догон хвостов»: повторная обработка логов с не финальными статусами,
#     у которых next_retry_at IS NULL или <= NOW().
#   • Деградация интеграций: сетевые/временные ошибки TON API не рушат цикл —
#     помечаются error_* и переигрываются отдельно.
#
# Запреты:
#   • Этот модуль не создаёт NFT и не меняет ставки генерации.
#   • Не трогает пользовательские балансы напрямую — только через Банк
#     (backend.app.services.transactions_service).
# =============================================================================

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, Optional, Tuple, List

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config_core import get_settings
from backend.app.core.logging_core import get_logger
from backend.app.deps import d8
from backend.app.services.transactions_service import (
    credit_user_from_bank,  # начисление EFHC пользователю (зеркально списывается у Банка)
)
from backend.app.services.shop_service import (
    ITEM_TYPE_EFHC_PACKAGE,
    ITEM_TYPE_NFT_VIP,
)
from backend.app.integrations.ton_api import TonAPIClient, TonAPIEvent  # клиент и DTO событий TON

logger = get_logger(__name__)
settings = get_settings()
SCHEMA = getattr(settings, "DB_SCHEMA_CORE", "efhc_core")

MAIN_TON_WALLET: str = getattr(settings, "TON_MAIN_WALLET", None) or getattr(settings, "MAIN_WALLET", "") or ""
if not MAIN_TON_WALLET:
    logger.warning("[WATCHER] TON_MAIN_WALLET/MAIN_WALLET is not set in config — watcher will not work correctly.")

# -----------------------------------------------------------------------------
# Регулярные выражения парсинга MEMO (строго по канону)
# -----------------------------------------------------------------------------
_RE_SIMPLE_EFHC = re.compile(r"^EFHC(?P<tgid>\d{1,20})$")  # EFHC<tgid>
_RE_SKU_EFHC = re.compile(
    r"^SKU:EFHC\|Q:(?P<qty>\d{1,12})\|TG:(?P<tgid>\d{1,20})$"
)  # SKU:EFHC|Q:<INT>|TG:<id>
_RE_SKU_NFT = re.compile(
    r"^SKU:NFT_VIP\|Q:(?P<qty>1)\|TG:(?P<tgid>\d{1,20})$"
)  # SKU:NFT_VIP|Q:1|TG:<id>

# -----------------------------------------------------------------------------
# Константы статусов логов inbox (тон-инбокс)
# -----------------------------------------------------------------------------
STATUS_RECEIVED = "received"  # запись создана/обновлена, ещё ничего не делали
STATUS_PARSED = "parsed"  # распознали тип/пользователя/параметры
STATUS_CREDITED = "credited"  # EFHC начислены успешно
STATUS_PAID_AUTO = "paid_auto"  # заказ найден и оплачен (для EFHC SKU)
STATUS_NFT_MANUAL = "paid_pending_manual"  # NFT-заявка создана/обновлена (ручная выдача)
STATUS_ERROR_MEMO = "error_bad_memo"  # нераспознаваемый MEMO
STATUS_ERROR_USER = "error_user_not_found"  # не нашли пользователя ни по кошельку, ни по MEMO
STATUS_ERROR_NET = "error_network_retry"  # сетевой/временный сбой внешнего API (TON)
STATUS_ERROR_SYS = "error_retry"  # общий сбой (ретрай)
STATUS_PENDING_MAP = "parsed_pending_mapping"  # простой EFHC-депозит без однозначного прайса — ждём маппинг

FINAL_STATUSES = {STATUS_CREDITED, STATUS_PAID_AUTO, STATUS_NFT_MANUAL}

# -----------------------------------------------------------------------------
# DTO входящей транзакции (если вызывают напрямую из scheduler без TonAPIEvent)
# -----------------------------------------------------------------------------
@dataclass
class IncomingTx:
    tx_hash: str
    from_address: str
    to_address: str
    amount: Decimal
    memo: str
    utime: int  # unixtime сек


# =============================================================================
# Вспомогательные функции: нормализация, парсинг MEMO, работа с логами,
# поиск пользователя/заказов
# =============================================================================

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _norm_addr(addr: str) -> str:
    """Простейшая нормализация TON-адреса (убираем пробелы)."""
    return (addr or "").replace(" ", "")


def _parse_memo(memo: str) -> Tuple[str, Dict[str, int]]:
    """
    Детерминированно распарсить MEMO по канону.

    Возвращает:
        (kind, params), где:
            kind ∈ {"simple_efhc", "sku_efhc", "sku_nft", "bad"}
            params:
              • simple_efhc: {"tgid": int}
              • sku_efhc   : {"tgid": int, "qty": int}
              • sku_nft    : {"tgid": int, "qty": int}
    """
    memo = (memo or "").strip()
    m = _RE_SIMPLE_EFHC.match(memo)
    if m:
        return "simple_efhc", {"tgid": int(m.group("tgid"))}
    m = _RE_SKU_EFHC.match(memo)
    if m:
        return "sku_efhc", {"tgid": int(m.group("tgid")), "qty": int(m.group("qty"))}
    m = _RE_SKU_NFT.match(memo)
    if m:
        return "sku_nft", {"tgid": int(m.group("tgid")), "qty": int(m.group("qty"))}
    return "bad", {}


async def _ton_log_upsert(
    db: AsyncSession,
    *,
    tx: IncomingTx | TonAPIEvent,
    status: str,
    note: Optional[str] = None,
    next_retry_at: Optional[datetime] = None,
) -> None:
    """
    Фиксирует/обновляет запись о транзакции в ton_inbox_logs (read-through).

    Идемпотентность:
      • ON CONFLICT (tx_hash) DO UPDATE ...
      • Одна строка на один хеш транзакции.
      • retries_count автоматически увеличивается для статусов error_*.
    """
    await db.execute(
        text(
            f"""
            INSERT INTO {SCHEMA}.ton_inbox_logs
              (tx_hash, from_address, to_address, amount, memo, utime, status,
               retries_count, last_error, next_retry_at, processed_at,
               created_at, updated_at)
            VALUES
              (:tx_hash, :from_addr, :to_addr, :amount, :memo,
               to_timestamp(:utime), :status,
               CASE WHEN :is_error THEN 1 ELSE 0 END,
               :note, :next_retry_at,
               CASE WHEN :status IN ('{STATUS_CREDITED}','{STATUS_PAID_AUTO}','{STATUS_NFT_MANUAL}')
                    THEN NOW() ELSE NULL END,
               NOW(), NOW())
            ON CONFLICT (tx_hash) DO UPDATE SET
              status        = EXCLUDED.status,
              retries_count = CASE
                                WHEN EXCLUDED.status LIKE 'error%%'
                                THEN {SCHEMA}.ton_inbox_logs.retries_count + 1
                                ELSE {SCHEMA}.ton_inbox_logs.retries_count
                              END,
              last_error    = CASE
                                WHEN EXCLUDED.status LIKE 'error%%'
                                THEN EXCLUDED.last_error
                                ELSE NULL
                              END,
              next_retry_at = EXCLUDED.next_retry_at,
              updated_at    = NOW(),
              processed_at  = CASE
                                WHEN EXCLUDED.status IN ('{STATUS_CREDITED}',
                                                          '{STATUS_PAID_AUTO}',
                                                          '{STATUS_NFT_MANUAL}')
                                THEN NOW()
                                ELSE {SCHEMA}.ton_inbox_logs.processed_at
                              END
            """
        ),
        {
            "tx_hash": tx.tx_hash,
            "from_addr": _norm_addr(tx.from_address),
            "to_addr": _norm_addr(tx.to_address),
            "amount": str(d8(tx.amount)),
            "memo": (tx.memo or "").strip(),
            "utime": int(getattr(tx, "utime", 0) or 0),
            "status": status,
            "is_error": status.startswith("error"),
            "note": (note or None),
            "next_retry_at": next_retry_at,
        },
    )


async def _ton_log_exists_final(db: AsyncSession, tx_hash: str) -> bool:
    """Проверка: транзакция уже обработана до финального статуса (read-through)."""
    row = await db.execute(
        text(f"SELECT status FROM {SCHEMA}.ton_inbox_logs WHERE tx_hash = :h LIMIT 1"),
        {"h": tx_hash},
    )
    rec = row.fetchone()
    if not rec:
        return False
    return rec[0] in FINAL_STATUSES


async def _find_user_by_wallet(db: AsyncSession, addr: str) -> Optional[int]:
    """
    Поиск пользователя по привязанному TON-кошельку.

    Канон:
      • Привязки хранятся в {SCHEMA}.user_wallets.
      • user_wallets.user_id = telegram_id пользователя.
      • Используем только is_active = TRUE и is_blocked = FALSE.
    """
    if not addr:
        return None
    naddr = _norm_addr(addr)
    row = await db.execute(
        text(
            f"""
            SELECT user_id
              FROM {SCHEMA}.user_wallets
             WHERE ton_address = :wa
               AND is_active   = TRUE
               AND is_blocked  = FALSE
             LIMIT 1
            """
        ),
        {"wa": naddr},
    )
    r = row.fetchone()
    return int(r[0]) if r and r[0] is not None else None


async def _find_user_by_telegram_id(db: AsyncSession, tgid: int) -> Optional[int]:
    """
    Поиск пользователя по telegram_id.

    Канон:
      • Во всех денежных сервисах user_id == telegram_id.
      • Здесь мы лишь проверяем наличие записи в users; если нет — возвращаем None.
    """
    row = await db.execute(
        text(
            f"""
            SELECT telegram_id
              FROM {SCHEMA}.users
             WHERE telegram_id = :tg
             LIMIT 1
            """
        ),
        {"tg": int(tgid)},
    )
    r = row.fetchone()
    return int(r[0]) if r and r[0] is not None else None


async def _find_pending_order(db: AsyncSession, *, user_id: int, qty: int) -> Optional[int]:
    """
    Поиск PENDING-заказа EFHC-пакета, соответствующего MEMO SKU:EFHC|Q:<qty>|TG:<id>.

    Логика:
      • Ищем активный товар с item_type = ITEM_TYPE_EFHC_PACKAGE и extra_json.quantity_efhc = qty.
      • Для найденного товара берём самый старый PENDING-заказ пользователя на этот item_id.
    """
    row = await db.execute(
        text(
            f"""
            SELECT o.id
              FROM {SCHEMA}.shop_orders o
              JOIN {SCHEMA}.shop_items i ON i.id = o.item_id
             WHERE o.user_id = :uid
               AND o.status  = 'PENDING'
               AND i.item_type = :itype
               AND COALESCE((i.extra_json->>'quantity_efhc')::bigint, 0) = :q
             ORDER BY o.created_at ASC, o.id ASC
             LIMIT 1
            """
        ),
        {"uid": int(user_id), "itype": ITEM_TYPE_EFHC_PACKAGE, "q": int(qty)},
    )
    r = row.fetchone()
    return int(r[0]) if r else None


async def _mark_order_paid_auto(db: AsyncSession, order_id: int, tx_hash: str) -> None:
    """Переводим заказ в PAID_AUTO и привязываем tx_hash (идемпотентно)."""
    await db.execute(
        text(
            f"""
            UPDATE {SCHEMA}.shop_orders
               SET status     = 'PAID_AUTO',
                   tx_hash    = COALESCE(tx_hash, :h),
                   updated_at = NOW()
             WHERE id = :oid
            """
        ),
        {"oid": int(order_id), "h": tx_hash},
    )


async def _create_nft_manual_request(
    db: AsyncSession,
    user_id: int,
    *,
    tx_hash: str,
    qty: int = 1,
) -> int:
    """
    NFT: создаём заявку PAID_PENDING_MANUAL в shop_orders.

    Канон:
      • Здесь нет автодоставки NFT — только заявка.
      • Админ вручную сопоставляет заявку с конкретным NFT и передаёт пользователю.
    """
    # Пытаемся найти NFT-карточку по item_type = ITEM_TYPE_NFT_VIP.
    row = await db.execute(
        text(
            f"""
            INSERT INTO {SCHEMA}.shop_orders
                (user_id, item_id, status, tx_hash, created_at, updated_at)
            SELECT :uid, i.id, 'PAID_PENDING_MANUAL', :h, NOW(), NOW()
              FROM {SCHEMA}.shop_items i
             WHERE i.item_type = :itype
               AND COALESCE(i.is_active, TRUE) = TRUE
             ORDER BY i.id ASC
             LIMIT 1
            ON CONFLICT DO NOTHING
            RETURNING id
            """
        ),
        {"uid": int(user_id), "h": tx_hash, "itype": ITEM_TYPE_NFT_VIP},
    )
    r = row.fetchone()
    if r:
        return int(r[0])

    # Если в каталоге нет NFT-позиции — создаём «обезличенную» заявку.
    row2 = await db.execute(
        text(
            f"""
            INSERT INTO {SCHEMA}.shop_orders
                (user_id, status, tx_hash, created_at, updated_at)
            VALUES (:uid, 'PAID_PENDING_MANUAL', :h, NOW(), NOW())
            RETURNING id
            """
        ),
        {"uid": int(user_id), "h": tx_hash},
    )
    r2 = row2.fetchone()
    return int(r2[0])


async def _resolve_simple_deposit_qty(db: AsyncSession, *, amount_ton: Decimal) -> Optional[int]:
    """
    Попытка разрешить простой депозит EFHC<tgid> через прайс EFHC-товара в каталоге.

    Правило:
      • Берём активный EFHC-товар (item_type = ITEM_TYPE_EFHC_PACKAGE) с заданной price_ton.
      • qty = floor(amount_ton / price_ton).
      • Если вычислить нельзя или qty <= 0 → возвращаем None (ждём ручной маппинг).
    """
    row = await db.execute(
        text(
            f"""
            SELECT price_ton
              FROM {SCHEMA}.shop_items
             WHERE item_type = :itype
               AND COALESCE(is_active, TRUE) = TRUE
               AND price_ton IS NOT NULL
             ORDER BY id ASC
             LIMIT 1
            """
        ),
        {"itype": ITEM_TYPE_EFHC_PACKAGE},
    )
    r = row.fetchone()
    if not r or r[0] is None:
        return None
    try:
        price = d8(r[0])
        if price <= 0:
            return None
        qty = int(d8(amount_ton) / price)
        return qty if qty > 0 else None
    except Exception:
        return None


# =============================================================================
# Ядро обработки одной транзакции (read-through идемпотентность)
# =============================================================================

async def process_incoming_tx(db: AsyncSession, tx: IncomingTx | TonAPIEvent) -> str:
    """
    Обрабатывает одну входящую транзакцию TON.

    Вход:
      • tx_hash, from_address, to_address, amount, memo, utime.

    Итог:
      • Статус из набора FINAL / ERROR_* / STATUS_PENDING_MAP.
      • Побочные эффекты:
          – запись/обновление {SCHEMA}.ton_inbox_logs,
          – начисление EFHC (credit_user_from_bank),
          – перевод заказа в PAID_AUTO,
          – создание заявки NFT (PAID_PENDING_MANUAL).

    Идемпотентность:
      • UNIQUE(tx_hash) в ton_inbox_logs.
      • Повторные вызовы для одной и той же транзакции безопасны.
    """
    # Нормализуем адрес назначения и отфильтровываем «чужие» транзакции.
    if MAIN_TON_WALLET and _norm_addr(getattr(tx, "to_address", "")) != _norm_addr(MAIN_TON_WALLET):
        # Мягкий skip: фиксировать в лог нет смысла, это не наш кошелёк.
        return "skip:not_our_wallet"

    # 1) Быстрый read-through: если уже финализировано — возврат статуса
    if await _ton_log_exists_final(db, tx.tx_hash):
        logger.debug("[WATCHER] tx %s already finalized", tx.tx_hash)
        return "already_final"

    # 2) Upsert «получили»
    await _ton_log_upsert(db, tx=tx, status=STATUS_RECEIVED)

    try:
        # 3) Идентификация пользователя: приоритет у привязанного кошелька
        user_id: Optional[int] = await _find_user_by_wallet(db, tx.from_address)
        kind: str = "bad"
        params: Dict[str, int] = {}

        if user_id is None:
            # Нет привязки кошелька → пробуем MEMO
            kind, params = _parse_memo(getattr(tx, "memo", "") or "")
            if kind == "bad":
                await _ton_log_upsert(
                    db,
                    tx=tx,
                    status=STATUS_ERROR_MEMO,
                    note="Unrecognized MEMO",
                    next_retry_at=None,
                )
                return STATUS_ERROR_MEMO

            tgid = params.get("tgid")
            if not tgid:
                await _ton_log_upsert(
                    db,
                    tx=tx,
                    status=STATUS_ERROR_USER,
                    note="Missing TG in memo",
                    next_retry_at=None,
                )
                return STATUS_ERROR_USER

            user_id = await _find_user_by_telegram_id(db, tgid)

        # Если и сейчас не нашли пользователя — это ошибка данных (без ретраев)
        if user_id is None:
            await _ton_log_upsert(
                db,
                tx=tx,
                status=STATUS_ERROR_USER,
                note="User not found by wallet/memo",
                next_retry_at=None,
            )
            return STATUS_ERROR_USER

        # 4) Разбор MEMO, если ещё не сделали (когда user найден по кошельку)
        if kind == "bad":
            kind, params = _parse_memo(getattr(tx, "memo", "") or "")

        await _ton_log_upsert(db, tx=tx, status=STATUS_PARSED)

        # 5) Ветвление по типу MEMO
        if kind == "sku_efhc":
            # Автодоставка EFHC-пакета
            qty = int(params.get("qty", 0))
            if qty <= 0:
                await _ton_log_upsert(db, tx=tx, status=STATUS_ERROR_MEMO, note="EFHC qty <= 0")
                return STATUS_ERROR_MEMO

            # 5.1) Найдём PENDING-заказ на этот пакет, если есть
            order_id = await _find_pending_order(db, user_id=user_id, qty=qty)
            if order_id:
                await _mark_order_paid_auto(db, order_id=order_id, tx_hash=tx.tx_hash)

            # 5.2) Начислим EFHC пользователю (1:1 по qty) через Банк
            await credit_user_from_bank(
                db=db,
                user_id=int(user_id),
                amount_efhc=d8(qty),
                reason="shop_auto_delivery",
                idempotency_key=f"ton:sku_efhc:{tx.tx_hash}",
            )

            status = STATUS_PAID_AUTO if order_id else STATUS_CREDITED
            await _ton_log_upsert(db, tx=tx, status=status)
            return status

        elif kind == "sku_nft":
            # 5.3) NFT — только заявка (ручная выдача)
            await _create_nft_manual_request(
                db,
                user_id=int(user_id),
                tx_hash=tx.tx_hash,
                qty=int(params.get("qty", 1)),
            )
            await _ton_log_upsert(db, tx=tx, status=STATUS_NFT_MANUAL)
            return STATUS_NFT_MANUAL

        elif kind == "simple_efhc":
            # 5.4) Простой депозит EFHC<tgid>: пытаемся вычислить количество по каталогу EFHC-пакетов
            qty = await _resolve_simple_deposit_qty(db, amount_ton=d8(tx.amount))
            if qty and qty > 0:
                await credit_user_from_bank(
                    db=db,
                    user_id=int(user_id),
                    amount_efhc=d8(qty),
                    reason="simple_deposit_auto",
                    idempotency_key=f"ton:simple:{tx.tx_hash}",
                )
                await _ton_log_upsert(db, tx=tx, status=STATUS_CREDITED)
                return STATUS_CREDITED

            # Нельзя однозначно вычислить — ждём ручной маппинг
            await _ton_log_upsert(
                db,
                tx=tx,
                status=STATUS_PENDING_MAP,
                note="Await mapping/pricing",
                next_retry_at=None,
            )
            return STATUS_PENDING_MAP

        else:
            # Неподдерживаемый формат (защитный кейс)
            await _ton_log_upsert(
                db,
                tx=tx,
                status=STATUS_ERROR_MEMO,
                note=f"Unsupported kind {kind}",
                next_retry_at=None,
            )
            return STATUS_ERROR_MEMO

    except Exception as e:
        # Общий сбой — помечаем и назначаем ретрай
        logger.exception("[WATCHER] process tx failed %s: %s", getattr(tx, "tx_hash", "?"), e)
        await _ton_log_upsert(
            db,
            tx=tx,
            status=STATUS_ERROR_SYS,
            note=str(e),
            next_retry_at=_now_utc() + timedelta(minutes=10),
        )
        return STATUS_ERROR_SYS


# =============================================================================
# Высокоуровневые циклы: чтение из TON API и догон «хвостов»
# =============================================================================

async def process_incoming_payments(
    db: AsyncSession,
    *,
    limit: int = 200,
    since_utime: Optional[int] = None,
) -> Dict[str, int]:
    """
    Получить из TON API порцию входящих платежей и обработать их идемпотентно.

    Вход:
      • limit, since_utime (unixtime). Если since_utime не задан — TonAPIClient
        сам решает, откуда начинать.

    Выход:
      • Счётчики по статусам.

    ИИ-защита:
      • Сетевые ошибки TON API не приводят к падению цикла — просто фиксируем
        error_network и возвращаем статистику.
    """
    stats: Dict[str, int] = {}
    client = TonAPIClient()

    if not MAIN_TON_WALLET:
        logger.warning("[WATCHER] MAIN_TON_WALLET is empty, skip fetching")
        return {"error_network": 1}

    try:
        events: List[TonAPIEvent] = await client.get_incoming_payments(
            to_address=MAIN_TON_WALLET,
            limit=int(limit),
            since_utime=since_utime,
        )
    except Exception as e:
        logger.warning("[WATCHER] TonAPI fetch failed: %s", e)
        return {"error_network": 1}

    for ev in events:
        st = await process_incoming_tx(db, ev)
        stats[st] = stats.get(st, 0) + 1

    return stats


async def reprocess_unfinished_logs(
    db: AsyncSession,
    *,
    limit: int = 500,
) -> Dict[str, int]:
    """
    «Догон» логов ton_inbox_logs с не финальными статусами, которым пришло время ретрая.

    Вход:
      • limit — ограничение на количество записей за проход.

    Выход:
      • Счётчики по статусам после повторной обработки.
    """
    stats: Dict[str, int] = {}
    row = await db.execute(
        text(
            f"""
            SELECT tx_hash,
                   from_address,
                   to_address,
                   amount,
                   memo,
                   EXTRACT(EPOCH FROM COALESCE(utime, NOW()))::bigint AS utime
              FROM {SCHEMA}.ton_inbox_logs
             WHERE status NOT IN (:s1, :s2, :s3)
               AND (next_retry_at IS NULL OR next_retry_at <= NOW())
             ORDER BY COALESCE(updated_at, created_at) ASC
             LIMIT :lim
            """
        ),
        {
            "s1": STATUS_CREDITED,
            "s2": STATUS_PAID_AUTO,
            "s3": STATUS_NFT_MANUAL,
            "lim": int(limit),
        },
    )
    items = row.fetchall()
    for r in items:
        tx = IncomingTx(
            tx_hash=str(r[0]),
            from_address=str(r[1] or ""),
            to_address=str(r[2] or ""),
            amount=d8(r[3] or 0),
            memo=str(r[4] or ""),
            utime=int(r[5] or 0),
        )
        st = await process_incoming_tx(db, tx)
        stats[st] = stats.get(st, 0) + 1
    return stats


# =============================================================================
# Утилиты для админских задач: сверка по времени и по конкретному tx_hash
# =============================================================================

async def reconcile_last_hours(
    db: AsyncSession,
    *,
    hours: int = 24,
    limit: int = 500,
) -> Dict[str, int]:
    """
    Защитный «догон» всех входящих за последние N часов напрямую из TON API.

    Вход:
      • hours — диапазон по времени.
      • limit — размер порции.

    Выход:
      • Счётчики по статусам.

    ИИ-защита:
      • process_incoming_tx идемпотентен, поэтому повторная обработка безопасна.
    """
    since = int((_now_utc() - timedelta(hours=hours)).timestamp())
    total: Dict[str, int] = {}
    page_since = since

    # Мягкое ограничение в 1000 страниц, чтобы не зациклиться
    for _ in range(0, 1000):
        stats = await process_incoming_payments(db, limit=limit, since_utime=page_since)
        if not stats:
            break
        for k, v in stats.items():
            total[k] = total.get(k, 0) + v
        await asyncio.sleep(0.1)

    return total


async def reprocess_single_tx(db: AsyncSession, tx_hash: str) -> str:
    """
    Повторная обработка конкретной транзакции по её tx_hash (из лога).

    Используется для админских сценариев или отладки.
    """
    row = await db.execute(
        text(
            f"""
            SELECT tx_hash,
                   from_address,
                   to_address,
                   amount,
                   memo,
                   EXTRACT(EPOCH FROM COALESCE(utime, NOW()))::bigint AS utime
              FROM {SCHEMA}.ton_inbox_logs
             WHERE tx_hash = :h
             LIMIT 1
            """
        ),
        {"h": tx_hash},
    )
    r = row.fetchone()
    if not r:
        return "not_found"

    tx = IncomingTx(
        tx_hash=str(r[0]),
        from_address=str(r[1] or ""),
        to_address=str(r[2] or ""),
        amount=d8(r[3] or 0),
        memo=str(r[4] or ""),
        utime=int(r[5] or 0),
    )
    return await process_incoming_tx(db, tx)


# =============================================================================
# Пояснения «для чайника»:
#   • Этот модуль не «создаёт деньги» сам — все начисления EFHC проходят через
#     банковский сервис transactions_service.credit_user_from_bank(...).
#   • Благодаря UNIQUE(tx_hash) и read-through та же транзакция TON не будет
#     начислена дважды: при повторе вернётся финальный статус.
#   • NFT не выдаётся автоматически: здесь создаётся только заявка со статусом
#     PAID_PENDING_MANUAL, которую обработает администратор.
#   • Если для простого депозита EFHC<tgid> нельзя однозначно определить объём
#     по прайсу EFHC-пакетов, запись получает статус parsed_pending_mapping и
#     ждёт ручного сопоставления.
#   • Любые временные/сетевые сбои помечаются error_* с next_retry_at, а
#     планировщик периодически вызывает reprocess_unfinished_logs() для догона.
# =============================================================================
