# -*- coding: utf-8 -*-
# backend/app/services/panels_service.py
# =============================================================================
# EFHC Bot — Сервис панелей (покупка, учёт, списки, архивирование)
# -----------------------------------------------------------------------------
# Назначение (коротко, 1–3 строки):
#   • Операции с «солнечными панелями» пользователя: покупка за EFHC, учёт активных
#     и архивных панелей, сводка для UI, безопасное архивирование просроченных.
#
# Канон/инварианты (строго):
#   • Цена панели фиксирована: 100 EFHC.
#   • Строгий порядок списания: СНАЧАЛА бонусный баланс, затем основной (1 операция — 2 дебета).
#   • Пользователь НИКОГДА не уходит в минус (жёсткий запрет): при дефиците — отказ.
#   • Банк EFHC может быть в минусе — операции НЕ блокируем (это не зона ответственности сервиса).
#   • Генерация энергии везде посекундная:
#       GEN_PER_SEC_BASE_KWH = 0.00000692 (без VIP),
#       GEN_PER_SEC_VIP_KWH  = 0.00000741 (VIP).
#     Ставка VIP определяется наличием NFT; панели хранят базовую ставку на момент создания,
#     но фактические начисления делает energy_service, исходя из текущего статуса VIP.
#   • Лимит активных панелей на пользователя: MAX_PANELS (по умолчанию 1000).
#   • Срок жизни панели: 180 дней; по окончании — перевод в архив (is_active=false).
#   • Все денежные движения только через единый банковский сервис (transactions_service).
#
# ИИ-защиты/самовосстановление:
#   • Блокировки на уровне SQL (FOR UPDATE SKIP LOCKED) минимизируют гонки за один и тот же user_id.
#   • Read-through идемпотентность делается в банковском сервисе по idempotency_key.
#   • Сервис «устойчив к частичным отказам»: если не удалось создать запись панели после успешных дебетов,
#     повтор с теми же idempotency_key безопасен (read-through банка).
#   • Для массовой покупки qty>1 — детерминированные ключи идемпотентности (suffix #1, #2, ...).
#
# Запреты:
#   • Никаких P2P-переводов. Никаких EFHC→kWh. Никаких «суточных» расчётов.
#   • Никаких прямых правок балансов — только через transactions_service.
# =============================================================================

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config_core import get_settings
from backend.app.core.logging_core import get_logger
from backend.app.deps import d8, encode_cursor, decode_cursor  # централизованные утилиты
from backend.app.services import transactions_service as bank

logger = get_logger(__name__)
settings = get_settings()
SCHEMA = getattr(settings, "DB_SCHEMA_CORE", "efhc_core")

# Константы канона (источник истины — settings/.env; названия строго канонические)
GEN_PER_SEC_BASE_KWH: Decimal = d8(
    getattr(settings, "GEN_PER_SEC_BASE_KWH", "0.00000692") or "0.00000692"
)
GEN_PER_SEC_VIP_KWH: Decimal = d8(
    getattr(settings, "GEN_PER_SEC_VIP_KWH", "0.00000741") or "0.00000741"
)
PANEL_PRICE: Decimal = d8(getattr(settings, "PANEL_PRICE", "100") or "100")
MAX_PANELS: int = int(getattr(settings, "MAX_PANELS", 1000) or 1000)
PANEL_LIFETIME_DAYS: int = int(getattr(settings, "PANEL_LIFETIME_DAYS", 180) or 180)

# -----------------------------------------------------------------------------
# Исключения домена (понятные коду роутов)
# -----------------------------------------------------------------------------
class PanelPurchaseError(Exception):
    """Общая ошибка покупки панели (детали в сообщении)."""


class UserInDebtError(PanelPurchaseError):
    """Исторический минус у пользователя — покупки за EFHC заблокированы."""


class PanelLimitExceeded(PanelPurchaseError):
    """Превышен лимит активных панелей."""


class IdempotencyRequiredError(PanelPurchaseError):
    """Для денежных операций обязателен idempotency_key."""

# -----------------------------------------------------------------------------
# DTO для ответов сервиса
# -----------------------------------------------------------------------------
@dataclass
class PurchaseResult:
    ok: bool
    qty: int
    total_spent_bonus: Decimal
    total_spent_main: Decimal
    created_panel_ids: List[int]
    detail: str


@dataclass
class PanelsSummary:
    active_count: int
    total_generated_by_panels: Decimal
    nearest_expire_at: Optional[datetime]


@dataclass
class PagedPanels:
    items: List[Dict[str, Any]]
    next_cursor: Optional[str]

# -----------------------------------------------------------------------------
# Внутренние утилиты
# -----------------------------------------------------------------------------
async def _lock_user_row(db: AsyncSession, user_id: int) -> Optional[Dict[str, Any]]:
    """
    Жёсткая блокировка записи пользователя, чтобы гасить гонки при одновременных покупках.
    Используем SKIP LOCKED: если кто-то держит блокировку — мы не ждём, а честно падаем;
    роутер может повторить позже.
    """
    row = await db.execute(
        text(
            f"""
            SELECT id, telegram_id, is_vip,
                   main_balance, bonus_balance,
                   total_generated_kwh, available_kwh
            FROM {SCHEMA}.users
            WHERE id = :uid
            FOR UPDATE SKIP LOCKED
            """
        ),
        {"uid": int(user_id)},
    )
    rec = row.fetchone()
    if not rec:
        return None
    return {
        "id": int(rec[0]),
        "telegram_id": int(rec[1]),
        "is_vip": bool(rec[2]),
        "main_balance": d8(rec[3]),
        "bonus_balance": d8(rec[4]),
        "total_generated_kwh": d8(rec[5]),
        "available_kwh": d8(rec[6]),
    }


async def _get_active_panels_count(db: AsyncSession, user_id: int) -> int:
    r = await db.execute(
        text(
            f"""
            SELECT COUNT(*) FROM {SCHEMA}.panels
            WHERE user_id = :uid AND is_active = TRUE
            """
        ),
        {"uid": int(user_id)},
    )
    return int(r.scalar() or 0)


async def _create_panel_record(db: AsyncSession, user_id: int) -> int:
    """
    Создаём одну панель. Базовая ставка хранится как GEN_PER_SEC_BASE_KWH.
    Фактическая генерация учитывает VIP в energy_service (канон).
    """
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(days=PANEL_LIFETIME_DAYS)
    r = await db.execute(
        text(
            f"""
            INSERT INTO {SCHEMA}.panels
              (user_id, created_at, expires_at, last_generated_at,
               base_gen_per_sec, generated_kwh, is_active, archived_at)
            VALUES
              (:uid, :created, :expires, :last_gen,
               :base_rate, 0, TRUE, NULL)
            RETURNING id
            """
        ),
        {
            "uid": int(user_id),
            "created": now,
            "expires": expires_at,
            "last_gen": now,  # стартовая метка; energy_service догонит по нужной логике
            "base_rate": str(GEN_PER_SEC_BASE_KWH),
        },
    )
    new_id = r.scalar()
    return int(new_id)

# -----------------------------------------------------------------------------
# Публичные функции сервиса
# -----------------------------------------------------------------------------
async def purchase_panel(
    db: AsyncSession,
    *,
    user_id: int,
    qty: int = 1,
    idempotency_key: Optional[str] = None,
) -> PurchaseResult:
    """
    Покупка панелей: списание EFHC (сначала бонус, затем основной), создание записей панелей.
    Идемпотентность обеспечивается банковским сервисом (read-through по idempotency_key).

    Вход:
      • user_id — целевой пользователь
      • qty — количество панелей (>=1)
      • idempotency_key — обязателен для денежных операций

    Побочные эффекты:
      • На сумму покупки EFHC списывается с пользователя и зачисляется Банку (две операции).
      • Создаются записи панелей (активные), срок 180 дней, базовая ставка GEN_PER_SEC_BASE_KWH.

    Исключения:
      • IdempotencyRequiredError — если не передан ключ идемпотентности
      • UserInDebtError — если у пользователя исторический минус (покупки за EFHC запрещены)
      • PanelLimitExceeded — если будет превышен лимит активных панелей
      • PanelPurchaseError — прочие ошибки
    """
    if not idempotency_key or not idempotency_key.strip():
        raise IdempotencyRequiredError("Idempotency-Key is required by canon for monetary operations.")

    if qty <= 0:
        raise PanelPurchaseError("Количество панелей должно быть >= 1.")

    # 1) Лочим пользователя (skip locked) — если занято, лучше повторить запрос из роутера/клиента
    user = await _lock_user_row(db, user_id=user_id)
    if not user:
        raise PanelPurchaseError("Пользователь не найден или временно заблокирован другой операцией.")

    # 2) Исторический минус блокирует покупки за EFHC (но не пополнения и не обмен kWh→EFHC)
    total_user = d8(user["main_balance"]) + d8(user["bonus_balance"])
    if total_user < d8("0"):
        raise UserInDebtError("У пользователя отрицательный баланс: покупки за EFHC запрещены до выхода в 0.")

    # 3) Проверка лимита активных панелей
    current_active = await _get_active_panels_count(db, user_id=user_id)
    if current_active + qty > MAX_PANELS:
        raise PanelLimitExceeded(f"Лимит активных панелей {MAX_PANELS} будет превышен.")

    # 4) Денежный поток: списываем EFHC в пользу Банка (сначала бонус, потом основной)
    #    Для детерминизма используем idempotency_key с суффиксами #1..#qty
    created_ids: List[int] = []
    total_spent_bonus = d8("0")
    total_spent_main = d8("0")

    price_per_panel = PANEL_PRICE

    for i in range(1, qty + 1):
        suffix_key = f"{idempotency_key}#{i}"
        # Сперва обновляем актуальные балансы (повторная покупка в цикле)
        user = await _lock_user_row(db, user_id=user_id)
        if not user:
            raise PanelPurchaseError("Не удалось обновить состояние пользователя.")

        bonus = d8(user["bonus_balance"])
        main = d8(user["main_balance"])

        spend_bonus = min(bonus, price_per_panel)
        spend_main = d8(price_per_panel) - spend_bonus

        # Жёсткий запрет на минус у пользователя: проверяем до реального списания
        if spend_main > main:
            raise PanelPurchaseError("Недостаточно EFHC для покупки панели без ухода в минус.")

        try:
            # Списываем бонусы (возврат в Банк) — если есть что списывать
            if spend_bonus > d8("0"):
                await bank.debit_user_bonus_to_bank(
                    db,
                    user_id=user_id,
                    amount=spend_bonus,
                    reason="panel_purchase",
                    idempotency_key=f"{suffix_key}:bonus",
                    meta={"module": "panels_service", "qty_index": i},
                )
                total_spent_bonus = d8(total_spent_bonus + spend_bonus)

            # Списываем основной баланс — остаток
            if spend_main > d8("0"):
                await bank.debit_user_to_bank(
                    db,
                    user_id=user_id,
                    amount=spend_main,
                    reason="panel_purchase",
                    idempotency_key=f"{suffix_key}:main",
                    meta={"module": "panels_service", "qty_index": i},
                )
                total_spent_main = d8(total_spent_main + spend_main)

            # Создаём запись панели
            new_panel_id = await _create_panel_record(db, user_id=user_id)
            created_ids.append(int(new_panel_id))

        except Exception as e:
            logger.exception("panel purchase failed at item %s: %s", i, e)
            # Т.к. банковский сервис read-through и идемпотентен, повторное выполнение с теми же ключами
            # безопасно. Здесь просто пробрасываем ошибку наружу — роутер сообщит пользователю,
            # а повтор позже восстановит состояние.
            raise PanelPurchaseError(f"Ошибка покупки панели (элемент {i}): {e}")

    return PurchaseResult(
        ok=True,
        qty=qty,
        total_spent_bonus=d8(total_spent_bonus),
        total_spent_main=d8(total_spent_main),
        created_panel_ids=created_ids,
        detail="Панели успешно куплены.",
    )

# -----------------------------------------------------------------------------
# Витрины/списки для UI (курсорная пагинация)
# -----------------------------------------------------------------------------
async def list_active_panels(
    db: AsyncSession,
    *,
    user_id: int,
    limit: int = 25,
    cursor: Optional[str] = None,
) -> PagedPanels:
    """
    Список активных панелей пользователя (is_active = TRUE), упорядоченных по (created_at DESC, id DESC).
    Возвращает items и next_cursor. Курсор — tuple (created_at_iso, id).
    """
    limit = max(1, min(100, int(limit)))
    c_where = ""
    params: Dict[str, Any] = {"uid": int(user_id), "lim": int(limit) + 1}  # берём +1 для определения наличия next

    if cursor:
        created_at_iso, last_id = decode_cursor(cursor)
        c_where = "AND (p.created_at, p.id) < (:c_at, :c_id)"
        params["c_at"] = created_at_iso
        params["c_id"] = int(last_id)

    r = await db.execute(
        text(
            f"""
            SELECT p.id, p.created_at, p.expires_at, p.base_gen_per_sec, p.generated_kwh
            FROM {SCHEMA}.panels p
            WHERE p.user_id = :uid
              AND p.is_active = TRUE
              {c_where}
            ORDER BY p.created_at DESC, p.id DESC
            LIMIT :lim
            """
        ),
        params,
    )
    rows = list(r.fetchall())
    has_next = len(rows) > limit
    if has_next:
        rows = rows[:limit]

    items: List[Dict[str, Any]] = []
    for row in rows:
        items.append(
            {
                "id": int(row[0]),
                "created_at": _as_iso(row[1]),
                "expires_at": _as_iso(row[2]),
                "base_gen_per_sec": str(d8(row[3])),
                "generated_kwh": str(d8(row[4])),
                "is_active": True,
            }
        )

    next_cursor = None
    if has_next and rows:
        last = rows[-1]
        next_cursor = encode_cursor((_as_iso(last[1]), int(last[0])))

    return PagedPanels(items=items, next_cursor=next_cursor)


async def list_archived_panels(
    db: AsyncSession,
    *,
    user_id: int,
    limit: int = 25,
    cursor: Optional[str] = None,
) -> PagedPanels:
    """
    Список архивных панелей (is_active = FALSE), упорядоченных по (expires_at DESC, id DESC).
    Возвращает items и next_cursor. Курсор — tuple (expires_at_iso, id).
    """
    limit = max(1, min(100, int(limit)))
    c_where = ""
    params: Dict[str, Any] = {"uid": int(user_id), "lim": int(limit) + 1}

    if cursor:
        expires_at_iso, last_id = decode_cursor(cursor)
        c_where = "AND (p.expires_at, p.id) < (:c_at, :c_id)"
        params["c_at"] = expires_at_iso
        params["c_id"] = int(last_id)

    r = await db.execute(
        text(
            f"""
            SELECT p.id, p.created_at, p.expires_at, p.base_gen_per_sec, p.generated_kwh, p.archived_at
            FROM {SCHEMA}.panels p
            WHERE p.user_id = :uid
              AND p.is_active = FALSE
              {c_where}
            ORDER BY p.expires_at DESC, p.id DESC
            LIMIT :lim
            """
        ),
        params,
    )
    rows = list(r.fetchall())
    has_next = len(rows) > limit
    if has_next:
        rows = rows[:limit]

    items: List[Dict[str, Any]] = []
    for row in rows:
        items.append(
            {
                "id": int(row[0]),
                "created_at": _as_iso(row[1]),
                "expires_at": _as_iso(row[2]),
                "base_gen_per_sec": str(d8(row[3])),
                "generated_kwh": str(d8(row[4])),
                "is_active": False,
                "archived_at": _as_iso(row[5]),
            }
        )

    next_cursor = None
    if has_next and rows:
        last = rows[-1]
        next_cursor = encode_cursor((_as_iso(last[2]), int(last[0])))

    return PagedPanels(items=items, next_cursor=next_cursor)


async def panels_summary(db: AsyncSession, *, user_id: int) -> PanelsSummary:
    """
    Короткая сводка для экрана Panels:
      • active_count — число активных панелей
      • total_generated_by_panels — суммарная генерация по полю панелей
      • nearest_expire_at — ближайшая дата истечения активной панели
    """
    r = await db.execute(
        text(
            f"""
            SELECT
              COUNT(*) FILTER (WHERE is_active = TRUE) AS active_count,
              COALESCE(SUM(generated_kwh), 0)        AS total_generated_by_panels,
              MIN(expires_at) FILTER (WHERE is_active = TRUE) AS nearest_expire_at
            FROM {SCHEMA}.panels
            WHERE user_id = :uid
            """
        ),
        {"uid": int(user_id)},
    )
    row = r.fetchone()
    return PanelsSummary(
        active_count=int(row[0] or 0),
        total_generated_by_panels=d8(row[1] or 0),
        nearest_expire_at=row[2],
    )

# -----------------------------------------------------------------------------
# Архивирование просроченных панелей (для планировщика)
# -----------------------------------------------------------------------------
async def archive_expired_panels(db: AsyncSession, *, limit: int = 1000) -> int:
    """
    Переводит просроченные панели в архив (is_active=false), проставляет archived_at.
    Возвращает количество переведённых записей.

    Реализация через CTE + RETURNING, без неканоничного UPDATE ... LIMIT:
      • выбираем порцию id под блокировку (SKIP LOCKED),
      • обновляем только эти записи,
      • считаем количество по числу возвращённых строк.
    """
    now = datetime.now(timezone.utc)
    r = await db.execute(
        text(
            f"""
            WITH cte AS (
                SELECT id
                FROM {SCHEMA}.panels
                WHERE is_active = TRUE
                  AND expires_at <= :now
                ORDER BY id
                LIMIT :lim
                FOR UPDATE SKIP LOCKED
            )
            UPDATE {SCHEMA}.panels AS p
               SET is_active = FALSE,
                   archived_at = :now
            FROM cte
            WHERE p.id = cte.id
            RETURNING p.id
            """
        ),
        {"now": now, "lim": int(limit)},
    )
    rows = r.fetchall()
    return len(rows)

# -----------------------------------------------------------------------------
# Вспомогательные
# -----------------------------------------------------------------------------
def _as_iso(dt: Optional[datetime]) -> Optional[str]:
    if not dt:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()

# =============================================================================
# Экспорт для import *
# =============================================================================
__all__ = [
    # исключения
    "PanelPurchaseError",
    "UserInDebtError",
    "PanelLimitExceeded",
    "IdempotencyRequiredError",
    # DTO
    "PurchaseResult",
    "PanelsSummary",
    "PagedPanels",
    # public API
    "purchase_panel",
    "list_active_panels",
    "list_archived_panels",
    "panels_summary",
    "archive_expired_panels",
]

# =============================================================================
# Пояснения «для чайника»:
#   • Почему списание «бонус→основной»? Это канон: бонусы тратятся первыми, и их номинал
#     возвращается в Банк, чтобы не создавать «скрытой эмиссии». Остаток добираем с
#     основного баланса — это обычный дебет в Банк.
#   • Почему панели хранят base_gen_per_sec? Чтобы не «зашивать» в запись состояние VIP.
#     За VIP отвечает energy_service: он начисляет по GEN_PER_SEC_VIP_KWH, если у
#     пользователя активен VIP (есть NFT), и по GEN_PER_SEC_BASE_KWH — иначе.
#   • Почему блокируем покупки при «историческом минусе»? Канон безопасности: систему
#     не «ломаем», но пользователь должен выйти из минуса сам (пополнение/обмен kWh→EFHC),
#     после чего покупки снова станут доступны.
#   • Почему нужны детерминированные idempotency_key "#i"? Чтобы повторная покупка
#     с qty>1 могла быть безопасно переисполнена без дублей даже при сетевых сбоях —
#     каждый элемент имеет свой уникальный ключ (read-through).
#   • Почему курсоры? На списках много записей; курсорная пагинация стабильнее и
#     дешевле смещений OFFSET при растущей БД.
# =============================================================================
