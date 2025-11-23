# -*- coding: utf-8 -*-
# backend/app/services/reports_service.py
# =============================================================================
# Назначение кода:
# Сводные отчёты и витрины для админ-панели EFHC Bot: агрегаты по пользователям,
# энергии и денежным потокам; метрики стабильности (идемпотентность, ретраи,
# дефицит банка), выгрузки логов с курсорной пагинацией, суточные отчёты,
# топ-пользователи по оборотам и сводка магазины (Shop).
#
# Канон/инварианты:
# • Денежные данные читаем только из БД-источников истины: users,
#   efhc_transfers_log, ton_inbox_logs, shop_orders, withdraw_requests, panels.
# • Расчёты НЕ изменяют балансы — только SELECT/агрегации.
# • Пагинация — курсорная (по (created_at,id)), без OFFSET.
#
# ИИ-защиты:
# • Любой запрос оборачивается в try/except; при частичной недоступности таблиц
#   отдаём частичные агрегаты с пометками degraded=True (не валим админку).
# • Библиотечные хелперы (d8, cursor кодек) берём из deps.py — единый источник.
# • Функции tolerant_* не бросают исключения наружу, а возвращают пустые наборы
#   или нули с флагами degraded.
#
# Запреты:
# • Никаких UPDATE/DELETE/INSERT — исключительно чтение.
# • Никаких «суточных» ставок: вся генерация в системе — по per-sec ставкам; здесь
#   отображаем только итоговые накопленные значения и агрегаты по времени.
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
from backend.app.deps import d8, encode_cursor, decode_cursor  # единые хелперы

logger = get_logger(__name__)
settings = get_settings()
SCHEMA = getattr(settings, "DB_SCHEMA_CORE", "efhc_core")

# -----------------------------------------------------------------------------
# DTO для дешёвой сериализации в роутер
# -----------------------------------------------------------------------------

@dataclass
class DashboardMetrics:
    users_total: int = 0
    panels_active: int = 0
    kwh_total_generated: Decimal = Decimal("0")
    efhc_bank_inflow: Decimal = Decimal("0")      # поступления в Банк (user→bank)
    efhc_bank_outflow: Decimal = Decimal("0")     # списания Банка (bank→user)
    withdraw_pending: int = 0
    withdraw_approved_24h: int = 0
    processed_with_deficit_24h: int = 0
    idempotency_conflicts_24h: int = 0
    ton_events_24h: int = 0
    degraded: bool = False                        # признак частичной деградации данных


@dataclass
class DailySummaryItem:
    day: str  # YYYY-MM-DD
    new_users: int
    efhc_inflow: Decimal
    efhc_outflow: Decimal
    withdraw_approved: int
    ton_events: int


@dataclass
class DailyAdminSummary:
    items: List[DailySummaryItem]
    degraded: bool = False


@dataclass
class TopBankUserFlow:
    user_id: int
    net_amount: Decimal      # bank_to_user - user_to_bank
    total_turnover: Decimal  # суммарный оборот по модулю
    transfers_count: int


@dataclass
class TopBankUsersReport:
    items: List[TopBankUserFlow]
    degraded: bool = False


@dataclass
class ShopSalesItem:
    currency: Optional[str]
    status: Optional[str]
    orders_count: int
    total_amount: Decimal


@dataclass
class ShopSalesSummary:
    items: List[ShopSalesItem]
    degraded: bool = False


# -----------------------------------------------------------------------------
# Витрина «Dashboard админки»
# -----------------------------------------------------------------------------

async def fetch_dashboard_metrics(
    db: AsyncSession,
    since_hours: int = 24,
) -> DashboardMetrics:
    """
    Возвращает агрегаты для админской главной панели.

    Вход:
      • since_hours — окно для «за последние N часов» по метрикам стабильности.

    Выход:
      • DashboardMetrics (все числа безопасно округлены d8 для EFHC/kWh).
    """
    m = DashboardMetrics()
    window_from = datetime.now(timezone.utc) - timedelta(hours=int(since_hours))

    try:
        # Пользователи всего
        q = await db.execute(text(f"SELECT COUNT(1) FROM {SCHEMA}.users"))
        m.users_total = int(q.scalar() or 0)
    except Exception as e:
        logger.warning("dashboard users_total degraded: %s", e)
        m.degraded = True

    try:
        # Активные панели (is_active=TRUE)
        q = await db.execute(text(f"SELECT COUNT(1) FROM {SCHEMA}.panels WHERE is_active = TRUE"))
        m.panels_active = int(q.scalar() or 0)
    except Exception as e:
        logger.warning("dashboard panels_active degraded: %s", e)
        m.degraded = True

    try:
        # Сумма тотальной генерации по всем пользователям
        q = await db.execute(text(f"SELECT COALESCE(SUM(total_generated_kwh),0) FROM {SCHEMA}.users"))
        m.kwh_total_generated = d8(q.scalar() or 0)
    except Exception as e:
        logger.warning("dashboard kwh_total_generated degraded: %s", e)
        m.degraded = True

    try:
        # Вход/выход EFHC у Банка: из журнала efhc_transfers_log
        # direction:
        #   • bank_to_user => outflow (банк -> пользователь)
        #   • user_to_bank => inflow (пользователь -> банк)
        q = await db.execute(
            text(
                f"""
                SELECT
                  COALESCE(SUM(CASE WHEN direction='bank_to_user' THEN amount ELSE 0 END), 0) AS bank_outflow,
                  COALESCE(SUM(CASE WHEN direction='user_to_bank' THEN amount ELSE 0 END), 0) AS bank_inflow
                FROM {SCHEMA}.efhc_transfers_log
                """
            )
        )
        row = q.fetchone()
        m.efhc_bank_outflow = d8(row[0] if row and row[0] is not None else 0)
        m.efhc_bank_inflow = d8(row[1] if row and row[1] is not None else 0)
    except Exception as e:
        logger.warning("dashboard bank flows degraded: %s", e)
        m.degraded = True

    try:
        # Заявки на вывод (pending) и одобренные за окно
        q = await db.execute(
            text(f"SELECT COUNT(1) FROM {SCHEMA}.withdraw_requests WHERE status='PENDING'")
        )
        m.withdraw_pending = int(q.scalar() or 0)

        q = await db.execute(
            text(
                f"""
                SELECT COUNT(1)
                FROM {SCHEMA}.withdraw_requests
                WHERE status='APPROVED' AND updated_at >= :from_ts
                """
            ),
            {"from_ts": window_from},
        )
        m.withdraw_approved_24h = int(q.scalar() or 0)
    except Exception as e:
        logger.warning("dashboard withdraw degraded: %s", e)
        m.degraded = True

    try:
        # Метрики стабильности за окно
        # processed_with_deficit: помечаем в efhc_transfers_log.extra_info JSONB
        q = await db.execute(
            text(
                f"""
                SELECT COUNT(1)
                FROM {SCHEMA}.efhc_transfers_log
                WHERE created_at >= :from_ts
                  AND (extra_info->>'bank_deficit_mode') = 'true'
                """
            ),
            {"from_ts": window_from},
        )
        m.processed_with_deficit_24h = int(q.scalar() or 0)
    except Exception as e:
        logger.warning("dashboard deficit metric degraded: %s", e)
        m.degraded = True

    try:
        # Конфликты идемпотентности: extra_info->>'idk_conflict'='true'
        q = await db.execute(
            text(
                f"""
                SELECT COUNT(1)
                FROM {SCHEMA}.efhc_transfers_log
                WHERE created_at >= :from_ts
                  AND (extra_info->>'idk_conflict') = 'true'
                """
            ),
            {"from_ts": window_from},
        )
        m.idempotency_conflicts_24h = int(q.scalar() or 0)
    except Exception as e:
        logger.warning("dashboard idk_conflicts degraded: %s", e)
        m.degraded = True

    try:
        # Сырые on-chain события за окно (для наблюдаемости вотчера)
        q = await db.execute(
            text(
                f"""
                SELECT COUNT(1)
                FROM {SCHEMA}.ton_inbox_logs
                WHERE created_at >= :from_ts
                """
            ),
            {"from_ts": window_from},
        )
        m.ton_events_24h = int(q.scalar() or 0)
    except Exception as e:
        logger.warning("dashboard ton_events degraded: %s", e)
        m.degraded = True

    return m


# -----------------------------------------------------------------------------
# Курсорные выгрузки логов (EFHC, TON) — для вкладок админки
# -----------------------------------------------------------------------------

async def list_efhc_logs(
    db: AsyncSession,
    limit: int = 50,
    cursor: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Курсорная выдача журнала EFHC переводов (efhc_transfers_log) для админки.
    Курсор формируется по (created_at, id) через encode_cursor/decode_cursor.
    OFFSET не используется.

    Выход:
      • items: список словарей (частичное представление лога),
      • next_cursor: str|None — для следующей страницы,
      • degraded: bool — флаг деградации.
    """
    limit = max(1, min(int(limit or 50), 200))

    after_created_at: Optional[str]
    after_id: Optional[int]
    if cursor:
        try:
            after_created_at, after_id = decode_cursor(cursor)
        except Exception as e:
            logger.warning("list_efhc_logs: bad cursor %s: %s", cursor, e)
            after_created_at, after_id = (None, None)
    else:
        after_created_at, after_id = (None, None)

    cond = "TRUE"
    params: Dict[str, Any] = {}
    if after_created_at is not None and after_id is not None:
        cond = "(created_at, id) > (:ca, :cid)"
        params.update({"ca": after_created_at, "cid": int(after_id)})

    sql = f"""
        SELECT id, user_id, amount, direction, balance_type, reason, idempotency_key,
               extra_info, created_at
        FROM {SCHEMA}.efhc_transfers_log
        WHERE {cond}
        ORDER BY created_at ASC, id ASC
        LIMIT :lim
    """
    params["lim"] = limit + 1  # на один больше для определения next_cursor

    try:
        rs = await db.execute(text(sql), params)
        rows = rs.fetchall()
    except Exception as e:
        logger.error("list_efhc_logs failed: %s", e)
        return {"items": [], "next_cursor": None, "degraded": True}

    items: List[Dict[str, Any]] = []
    for r in rows[:limit]:
        created_at = r[8]
        items.append(
            {
                "id": int(r[0]),
                "user_id": int(r[1]) if r[1] is not None else None,
                "amount": str(d8(r[2] or 0)),
                "direction": r[3],
                "balance_type": r[4],
                "reason": r[5],
                "idempotency_key": r[6],
                "extra_info": r[7],
                "created_at": created_at.isoformat() if created_at else None,
            }
        )

    next_cursor: Optional[str] = None
    if len(rows) > limit:
        last = rows[limit - 1]
        last_created = last[8]
        if last_created:
            next_cursor = encode_cursor((last_created.isoformat(), int(last[0])))

    return {"items": items, "next_cursor": next_cursor, "degraded": False}


async def list_ton_logs(
    db: AsyncSession,
    limit: int = 50,
    cursor: Optional[str] = None,
    status: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Курсорная выдача сырых TON-событий (ton_inbox_logs) для админки.

    Фильтры:
      • status — опционально (received/parsed/credited/error_*).

    Выход:
      • items, next_cursor, degraded — аналогично list_efhc_logs.
    """
    limit = max(1, min(int(limit or 50), 200))

    after_created_at: Optional[str]
    after_id: Optional[int]
    if cursor:
        try:
            after_created_at, after_id = decode_cursor(cursor)
        except Exception as e:
            logger.warning("list_ton_logs: bad cursor %s: %s", cursor, e)
            after_created_at, after_id = (None, None)
    else:
        after_created_at, after_id = (None, None)

    cond = "TRUE"
    params: Dict[str, Any] = {}
    if status:
        cond += " AND status = :st"
        params["st"] = status
    if after_created_at is not None and after_id is not None:
        cond += " AND (created_at, id) > (:ca, :cid)"
        params.update({"ca": after_created_at, "cid": int(after_id)})

    sql = f"""
        SELECT id, tx_hash, from_address, to_address, amount, memo, status,
               retries_count, next_retry_at, last_error, created_at
        FROM {SCHEMA}.ton_inbox_logs
        WHERE {cond}
        ORDER BY created_at ASC, id ASC
        LIMIT :lim
    """
    params["lim"] = limit + 1

    try:
        rs = await db.execute(text(sql), params)
        rows = rs.fetchall()
    except Exception as e:
        logger.error("list_ton_logs failed: %s", e)
        return {"items": [], "next_cursor": None, "degraded": True}

    items: List[Dict[str, Any]] = []
    for r in rows[:limit]:
        created_at = r[10]
        items.append(
            {
                "id": int(r[0]),
                "tx_hash": r[1],
                "from_address": r[2],
                "to_address": r[3],
                "amount": str(d8(r[4] or 0)),
                "memo": r[5],
                "status": r[6],
                "retries_count": int(r[7] or 0),
                "next_retry_at": r[8].isoformat() if r[8] else None,
                "last_error": r[9],
                "created_at": created_at.isoformat() if created_at else None,
            }
        )

    next_cursor: Optional[str] = None
    if len(rows) > limit:
        last = rows[limit - 1]
        last_created = last[10]
        if last_created:
            next_cursor = encode_cursor((last_created.isoformat(), int(last[0])))

    return {"items": items, "next_cursor": next_cursor, "degraded": False}


# -----------------------------------------------------------------------------
# Диагностика «лагов» догонов (метрика самовосстановления)
# -----------------------------------------------------------------------------

async def lag_diagnostics(
    db: AsyncSession,
    since_hours: int = 24,
) -> Dict[str, Any]:
    """
    Оценивает «лаг» между появлением on-chain событий и их финализацией внутри EFHC.
    Возвращает усреднённые и максимальные задержки по окну.

    Примечание: корректность метрики зависит от заполнения полей processed_at в ton_inbox_logs.
    """
    from_ts = datetime.now(timezone.utc) - timedelta(hours=int(since_hours))
    sql = f"""
        SELECT
          EXTRACT(EPOCH FROM (processed_at - created_at)) AS lag_sec
        FROM {SCHEMA}.ton_inbox_logs
        WHERE created_at >= :from_ts
          AND processed_at IS NOT NULL
          AND status LIKE 'credited%%'
    """

    try:
        rs = await db.execute(text(sql), {"from_ts": from_ts})
        lags = [float(r[0]) for r in rs.fetchall() if r[0] is not None]
    except Exception as e:
        logger.warning("lag_diagnostics degraded: %s", e)
        return {"avg_sec": None, "max_sec": None, "count": 0, "degraded": True}

    if not lags:
        return {"avg_sec": None, "max_sec": None, "count": 0, "degraded": False}

    avg_sec = sum(lags) / len(lags)
    max_sec = max(lags)
    return {
        "avg_sec": round(avg_sec, 2),
        "max_sec": round(max_sec, 2),
        "count": len(lags),
        "degraded": False,
    }


# -----------------------------------------------------------------------------
# Суточные агрегаты для админки (для графиков/отчётов)
# -----------------------------------------------------------------------------

async def daily_admin_summary(
    db: AsyncSession,
    days: int = 7,
) -> DailyAdminSummary:
    """
    Суточная сводка за последние N дней (включая сегодня):

      • new_users         — регистрации (users.created_at).
      • efhc_inflow       — EFHC user→bank по дням.
      • efhc_outflow      — EFHC bank→user по дням.
      • withdraw_approved — количество одобренных выводов.
      • ton_events        — количество TON-событий.

    Только чтение; при частичных сбоях degraded=True.
    """
    days = max(1, min(int(days or 7), 90))
    from_ts = datetime.now(timezone.utc) - timedelta(days=days - 1)
    summary = DailyAdminSummary(items=[], degraded=False)

    # Дни — единый каркас YYYY-MM-DD
    base_days: Dict[str, DailySummaryItem] = {}
    for i in range(days):
        day = (from_ts + timedelta(days=i)).date().isoformat()
        base_days[day] = DailySummaryItem(
            day=day,
            new_users=0,
            efhc_inflow=Decimal("0"),
            efhc_outflow=Decimal("0"),
            withdraw_approved=0,
            ton_events=0,
        )

    # new_users
    try:
        rs = await db.execute(
            text(
                f"""
                SELECT DATE(created_at) AS d, COUNT(1)
                FROM {SCHEMA}.users
                WHERE created_at >= :from_ts
                GROUP BY DATE(created_at)
                """
            ),
            {"from_ts": from_ts},
        )
        for d, cnt in rs.fetchall():
            key = d.isoformat()
            if key in base_days:
                base_days[key].new_users = int(cnt or 0)
    except Exception as e:
        logger.warning("daily_admin_summary: new_users degraded: %s", e)
        summary.degraded = True

    # EFHC inflow/outflow по дням
    try:
        rs = await db.execute(
            text(
                f"""
                SELECT
                  DATE(created_at) AS d,
                  COALESCE(SUM(CASE WHEN direction='bank_to_user' THEN amount ELSE 0 END),0) AS outflow,
                  COALESCE(SUM(CASE WHEN direction='user_to_bank' THEN amount ELSE 0 END),0) AS inflow
                FROM {SCHEMA}.efhc_transfers_log
                WHERE created_at >= :from_ts
                GROUP BY DATE(created_at)
                """
            ),
            {"from_ts": from_ts},
        )
        for d, outflow, inflow in rs.fetchall():
            key = d.isoformat()
            if key in base_days:
                base_days[key].efhc_outflow = d8(outflow or 0)
                base_days[key].efhc_inflow = d8(inflow or 0)
    except Exception as e:
        logger.warning("daily_admin_summary: efhc flows degraded: %s", e)
        summary.degraded = True

    # withdraw_approved
    try:
        rs = await db.execute(
            text(
                f"""
                SELECT DATE(updated_at) AS d, COUNT(1)
                FROM {SCHEMA}.withdraw_requests
                WHERE status='APPROVED' AND updated_at >= :from_ts
                GROUP BY DATE(updated_at)
                """
            ),
            {"from_ts": from_ts},
        )
        for d, cnt in rs.fetchall():
            key = d.isoformat()
            if key in base_days:
                base_days[key].withdraw_approved = int(cnt or 0)
    except Exception as e:
        logger.warning("daily_admin_summary: withdraw degraded: %s", e)
        summary.degraded = True

    # ton_events
    try:
        rs = await db.execute(
            text(
                f"""
                SELECT DATE(created_at) AS d, COUNT(1)
                FROM {SCHEMA}.ton_inbox_logs
                WHERE created_at >= :from_ts
                GROUP BY DATE(created_at)
                """
            ),
            {"from_ts": from_ts},
        )
        for d, cnt in rs.fetchall():
            key = d.isoformat()
            if key in base_days:
                base_days[key].ton_events = int(cnt or 0)
    except Exception as e:
        logger.warning("daily_admin_summary: ton_events degraded: %s", e)
        summary.degraded = True

    summary.items = list(base_days.values())
    return summary


# -----------------------------------------------------------------------------
# Топ пользователей по оборотам EFHC (для поиска «китов» и аномалий)
# -----------------------------------------------------------------------------

async def top_bank_users(
    db: AsyncSession,
    *,
    since_hours: int = 24,
    limit: int = 50,
) -> TopBankUsersReport:
    """
    Возвращает топ пользователей по обороту EFHC за окно since_hours.

      • net_amount      — чистое изменение (bank_to_user - user_to_bank).
      • total_turnover  — суммарный оборот по модулю.
      • transfers_count — количество операций в логе.

    Только чтение; при сбое degraded=True.
    """
    limit = max(1, min(int(limit or 50), 200))
    from_ts = datetime.now(timezone.utc) - timedelta(hours=int(since_hours))
    report = TopBankUsersReport(items=[], degraded=False)

    sql = f"""
        SELECT
          user_id,
          COALESCE(SUM(CASE WHEN direction='bank_to_user' THEN amount ELSE 0 END),0) AS to_user,
          COALESCE(SUM(CASE WHEN direction='user_to_bank' THEN amount ELSE 0 END),0) AS to_bank,
          COUNT(1) AS cnt,
          COALESCE(SUM(ABS(amount)),0) AS turnover
        FROM {SCHEMA}.efhc_transfers_log
        WHERE created_at >= :from_ts
          AND user_id IS NOT NULL
        GROUP BY user_id
        ORDER BY turnover DESC
        LIMIT :lim
    """

    try:
        rs = await db.execute(text(sql), {"from_ts": from_ts, "lim": limit})
        for user_id, to_user, to_bank, cnt, turnover in rs.fetchall():
            user_id_int = int(user_id)
            to_user_dec = d8(to_user or 0)
            to_bank_dec = d8(to_bank or 0)
            turnover_dec = d8(turnover or 0)
            net = d8(to_user_dec - to_bank_dec)
            report.items.append(
                TopBankUserFlow(
                    user_id=user_id_int,
                    net_amount=net,
                    total_turnover=turnover_dec,
                    transfers_count=int(cnt or 0),
                )
            )
    except Exception as e:
        logger.warning("top_bank_users degraded: %s", e)
        report.degraded = True

    return report


# -----------------------------------------------------------------------------
# Сводка продаж Shop (по валютам и статусам)
# -----------------------------------------------------------------------------

async def shop_sales_summary(
    db: AsyncSession,
    *,
    since_hours: int = 24,
) -> ShopSalesSummary:
    """
    Сводка заказов магазина за окно since_hours:

      • агрегация по (expected_currency, status)
      • количество заказов
      • сумма expected_amount

    Только SELECT; при сбое degraded=True.
    """
    from_ts = datetime.now(timezone.utc) - timedelta(hours=int(since_hours))
    summary = ShopSalesSummary(items=[], degraded=False)

    sql = f"""
        SELECT
          expected_currency,
          status,
          COUNT(1) AS orders_count,
          COALESCE(SUM(expected_amount),0) AS total_amount
        FROM {SCHEMA}.shop_orders
        WHERE created_at >= :from_ts
        GROUP BY expected_currency, status
        ORDER BY expected_currency NULLS LAST, status NULLS LAST
    """

    try:
        rs = await db.execute(text(sql), {"from_ts": from_ts})
        for currency, status, cnt, total in rs.fetchall():
            summary.items.append(
                ShopSalesItem(
                    currency=currency,
                    status=status,
                    orders_count=int(cnt or 0),
                    total_amount=d8(total or 0),
                )
            )
    except Exception as e:
        logger.warning("shop_sales_summary degraded: %s", e)
        summary.degraded = True

    return summary


# -----------------------------------------------------------------------------
# Экспортируемый API
# -----------------------------------------------------------------------------

__all__ = [
    # Dashboard
    "DashboardMetrics",
    "fetch_dashboard_metrics",
    # Логи
    "list_efhc_logs",
    "list_ton_logs",
    # Лаги
    "lag_diagnostics",
    # Суточные отчёты
    "DailySummaryItem",
    "DailyAdminSummary",
    "daily_admin_summary",
    # Топы по банку
    "TopBankUserFlow",
    "TopBankUsersReport",
    "top_bank_users",
    # Shop
    "ShopSalesItem",
    "ShopSalesSummary",
    "shop_sales_summary",
]
