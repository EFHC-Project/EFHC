# -*- coding: utf-8 -*-
# backend/app/services/tasks_service.py
# =============================================================================
# EFHC Bot — сервис заданий (Tasks)
# -----------------------------------------------------------------------------
# Что умеет:
#   • Список доступных заданий пользователю (cursor-based).
#   • Завершение задания:
#       - join_channel — автопроверка подписки на канал(ы) через Telegram Bot API.
#       - watch_ad     — постановка в pending_check (начисление после верификации колбэка).
#   • История бонусов за задания (cursor-based).
#
# Канон:
#   • Любые бонусы начисляются ТОЛЬКО через банковский сервис (EFHC Банк) в bonus_balance,
#     с жёсткой идемпотентностью по idempotency_key.
#   • Пользователь НИКОГДА не уходит в минус.
#   • P2P запрещён. Только «пользователь ↔ банк».
#
# ИИ-надёжность:
#   • Повторы с тем же idempotency_key безопасны (replay).
#   • Мягкие сбои — не ломают поток: статусы pending_check/cooldown.
#   • Все операции протоколируются в лог-таблицах (UNIQUE по idempotency_key).
# =============================================================================

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config_core import get_settings
from backend.app.core.logging_core import get_logger
from backend.app.deps import d8
from backend.app.integrations.telegram_bot_api import TelegramAPI
from backend.app.services.transactions_service import credit_user_bonus_from_bank

logger = get_logger(__name__)
settings = get_settings()
SCHEMA = getattr(settings, "DB_SCHEMA_CORE", "efhc_core")

# -----------------------------------------------------------------------------
# Внутренние DTO для страниц
# -----------------------------------------------------------------------------

@dataclass
class TaskItem:
    task_code: str
    title: str
    description: Optional[str]
    reward_bonus_efhc: Decimal
    is_repeatable: bool
    user_status: str           # available | done | cooldown | pending_check | disabled
    icon: Optional[str] = None
    meta: Optional[Dict[str, Any]] = None

@dataclass
class TasksPage:
    items: List[TaskItem]
    next_after: Optional[Dict[str, Any]]

@dataclass
class TaskRewardLogItem:
    id: int
    ts: datetime
    task_code: str
    amount_bonus_efhc: Decimal
    idempotency_key: str

@dataclass
class TaskRewardsLogPage:
    items: List[TaskRewardLogItem]
    next_after_id: Optional[int]

# -----------------------------------------------------------------------------
# SQL — осознанно без ORM, ради прозрачности и контроля
# Таблицы (миграции должны это создать):
#   • {schema}.tasks
#       (task_code TEXT PK, title TEXT, description TEXT NULL, reward_bonus_efhc NUMERIC(30,8),
#        is_repeatable BOOL, is_active BOOL, cooldown_sec INT NULL, type TEXT, icon TEXT NULL, meta JSONB NULL)
#   • {schema}.user_tasks_status
#       (user_id BIGINT, task_code TEXT, status TEXT, last_completed_at TIMESTAMPTZ NULL,
#        next_available_at TIMESTAMPTZ NULL, UNIQUE(user_id, task_code))
#   • {schema}.task_rewards_log
#       (id BIGSERIAL PK, user_id BIGINT, ts TIMESTAMPTZ, task_code TEXT,
#        amount_bonus_efhc NUMERIC(30,8), idempotency_key TEXT UNIQUE)
#   • {schema}.task_complete_lock
#       (id BIGSERIAL PK, idempotency_key TEXT UNIQUE, user_id BIGINT, task_code TEXT,
#        created_at TIMESTAMPTZ, updated_at TIMESTAMPTZ, credited BOOL DEFAULT FALSE)
# -----------------------------------------------------------------------------

_SQL_LIST_TASKS = text(
    f"""
    SELECT task_code, title, description, reward_bonus_efhc, is_repeatable, is_active,
           COALESCE(cooldown_sec,0) AS cooldown_sec, type, icon, meta
      FROM {SCHEMA}.tasks
     WHERE is_active = TRUE
       AND (:after_code IS NULL OR task_code > :after_code)
     ORDER BY task_code ASC
     LIMIT :lim
    """
)

_SQL_USER_TASK_STATUS = text(
    f"""
    SELECT status, last_completed_at, next_available_at
      FROM {SCHEMA}.user_tasks_status
     WHERE user_id = :uid AND task_code = :code
     LIMIT 1
    """
)

_SQL_UPSERT_USER_TASK_STATUS = text(
    f"""
    INSERT INTO {SCHEMA}.user_tasks_status
      (user_id, task_code, status, last_completed_at, next_available_at)
    VALUES
      (:uid, :code, :status, :lca, :naa)
    ON CONFLICT (user_id, task_code) DO UPDATE
       SET status = EXCLUDED.status,
           last_completed_at = EXCLUDED.last_completed_at,
           next_available_at = EXCLUDED.next_available_at
    """
)

_SQL_INSERT_REWARD_LOG = text(
    f"""
    INSERT INTO {SCHEMA}.task_rewards_log
      (user_id, ts, task_code, amount_bonus_efhc, idempotency_key)
    VALUES
      (:uid, NOW(), :code, :amt, :idk)
    ON CONFLICT (idempotency_key) DO NOTHING
    """
)

_SQL_SELECT_REWARD_LOG_BY_IDK = text(
    f"""
    SELECT id, user_id, ts, task_code, amount_bonus_efhc, idempotency_key
      FROM {SCHEMA}.task_rewards_log
     WHERE idempotency_key = :idk
     LIMIT 1
    """
)

_SQL_LIST_REWARD_LOG = text(
    f"""
    SELECT id, ts, task_code, amount_bonus_efhc, idempotency_key
      FROM {SCHEMA}.task_rewards_log
     WHERE user_id = :uid
       AND (:after_id IS NULL OR id < :after_id)
     ORDER BY id DESC
     LIMIT :lim
    """
)

_SQL_LOCK_UPSERT = text(
    f"""
    INSERT INTO {SCHEMA}.task_complete_lock
      (idempotency_key, user_id, task_code, created_at, updated_at, credited)
    VALUES
      (:idk, :uid, :code, NOW(), NOW(), FALSE)
    ON CONFLICT (idempotency_key) DO NOTHING
    """
)

_SQL_LOCK_SELECT_FOR_UPDATE = text(
    f"""
    SELECT id, user_id, task_code, credited
      FROM {SCHEMA}.task_complete_lock
     WHERE idempotency_key = :idk
     FOR UPDATE
    """
)

_SQL_LOCK_SET_CREDITED = text(
    f"""
    UPDATE {SCHEMA}.task_complete_lock
       SET credited = TRUE, updated_at = NOW()
     WHERE idempotency_key = :idk
    """
)

_SQL_GET_TASK = text(
    f"""
    SELECT task_code, title, description, reward_bonus_efhc, is_repeatable, is_active,
           COALESCE(cooldown_sec,0) AS cooldown_sec, type, icon, meta
      FROM {SCHEMA}.tasks
     WHERE task_code = :code
     LIMIT 1
    """
)

# -----------------------------------------------------------------------------
# Вспомогательные функции
# -----------------------------------------------------------------------------

def _now_utc() -> datetime:
    return datetime.now(tz=timezone.utc)

def _as_task_item_row(row: Any, user_status: str) -> TaskItem:
    return TaskItem(
        task_code=str(row[0]),
        title=str(row[1]),
        description=(row[2] if row[2] is not None else None),
        reward_bonus_efhc=d8(row[3] or 0),
        is_repeatable=bool(row[4]),
        user_status=user_status,
        icon=(row[8] if row[8] is not None else None),
        meta=(row[9] if row[9] is not None else None),
    )

def _calc_user_status(base_status: Optional[str], next_available_at: Optional[datetime]) -> str:
    """
    Нормализуем статус в один из: available | done | cooldown | pending_check | disabled.
    Если next_available_at в будущем — cooldown, иначе оставляем base_status или available.
    """
    if base_status == "pending_check":
        return "pending_check"
    if next_available_at and next_available_at > _now_utc():
        return "cooldown"
    return base_status or "available"

# -----------------------------------------------------------------------------
# Публичное API сервиса — список заданий
# -----------------------------------------------------------------------------

async def list_available_tasks(
    db: AsyncSession,
    *,
    user_id: int,
    limit: int = 50,
    after: Optional[Dict[str, Any]] = None,
) -> TasksPage:
    """
    Cursor-based список активных заданий для пользователя.
    Курсор: {"after_code": "<последний task_code>"}.
    """
    lim = max(1, min(200, int(limit)))
    after_code = None
    if after:
        after_code = after.get("after_code")

    rows = (await db.execute(_SQL_LIST_TASKS, {"after_code": after_code, "lim": lim})).fetchall()
    items: List[TaskItem] = []
    last_code: Optional[str] = None

    for r in rows:
        code = str(r[0])
        st_row = (await db.execute(_SQL_USER_TASK_STATUS, {"uid": int(user_id), "code": code})).fetchone()
        base_status = st_row[0] if st_row else None
        next_av = st_row[2] if st_row else None
        status_norm = _calc_user_status(base_status, next_av)
        items.append(_as_task_item_row(r, status_norm))
        last_code = code

    next_after = {"after_code": last_code} if (rows and len(rows) == lim) else None
    return TasksPage(items=items, next_after=next_after)

# -----------------------------------------------------------------------------
# Публичное API сервиса — завершение задания (join_channel / watch_ad)
# -----------------------------------------------------------------------------

async def complete_task(
    db: AsyncSession,
    *,
    user_id: int,
    task_code: str,
    proof: Dict[str, Any],
    idempotency_key: str,
) -> Dict[str, Any]:
    """
    Идемпотентное завершение задания:
      • Регистрируем idempotency_key в lock-таблице (UNIQUE).
      • Определяем тип задания (tasks.type).
      • Выполняем доменную проверку:
          - join_channel → TelegramAPI.batch_is_user_subscribed(...)
          - watch_ad     → ставим pending_check (начисление по колбэку провайдера).
      • При успешной проверке → кредит из Банка в bonus_balance (idempotent).
      • Обновляем user_tasks_status (done или cooldown).
      • Пишем task_rewards_log (UNIQUE idempotency_key).
    Возвращает dict с полями ok, task_code, reward_bonus_efhc, user_bonus_balance, replayed.
    """
    if not idempotency_key or not idempotency_key.strip():
        raise ValueError("idempotency_key is required")
    idk = idempotency_key.strip()

    # 0) Регистрируем ключ (если он уже есть — безопасный replay)
    try:
        await db.begin()
        await db.execute(_SQL_LOCK_UPSERT, {"idk": idk, "uid": int(user_id), "code": task_code})
        row = await db.execute(_SQL_LOCK_SELECT_FOR_UPDATE, {"idk": idk})
        lock = row.fetchone()
        if not lock:
            await db.rollback()
            raise RuntimeError("failed to acquire tasks lock (race)")
        _, _, _, credited = lock
        if credited:
            await db.commit()
            prev = (await db.execute(_SQL_SELECT_REWARD_LOG_BY_IDK, {"idk": idk})).fetchone()
            if prev:
                return {
                    "ok": True,
                    "task_code": str(prev[3]),
                    "reward_bonus_efhc": d8(prev[4] or 0),
                    "user_bonus_balance": None,
                    "replayed": True,
                }
            return {
                "ok": True,
                "task_code": task_code,
                "reward_bonus_efhc": Decimal("0"),
                "user_bonus_balance": None,
                "replayed": True,
            }
        await db.commit()
    except Exception:
        try:
            await db.rollback()
        except Exception:
            pass
        raise

    # 1) Загружаем задание
    task_row = (await db.execute(_SQL_GET_TASK, {"code": task_code})).fetchone()
    if not task_row:
        raise ValueError("task not found")
    if not bool(task_row[5]):  # is_active
        raise ValueError("task is disabled")

    t_type = str(task_row[7] or "").strip()  # "join_channel" | "watch_ad" | ...
    reward = d8(task_row[3] or 0)
    cooldown_sec = int(task_row[6] or 0)
    meta = task_row[9] or {}

    # 2) Проверяем персональный статус (cooldown/one-time)
    st_row = (await db.execute(_SQL_USER_TASK_STATUS, {"uid": int(user_id), "code": task_code})).fetchone()
    last_completed_at = st_row[1] if st_row else None
    next_available_at = st_row[2] if st_row else None
    if next_available_at and next_available_at > _now_utc():
        raise ValueError("task is in cooldown")
    if st_row and (not bool(task_row[4])):  # is_repeatable == False, уже делал
        raise ValueError("task is one-time and already completed")

    # 3) Доменная проверка
    if t_type == "join_channel":
        # Ожидаем список каналов в meta["channels"] или резервно — из настроек REQUIRED_CHANNELS
        if isinstance(meta, dict) and "channels" in meta and isinstance(meta["channels"], list):
            required = [str(x).strip() for x in meta["channels"] if str(x).strip()]
        else:
            env_raw = getattr(settings, "REQUIRED_CHANNELS", "") or ""
            required = [x.strip() for x in env_raw.split(",") if x.strip()]

        if not required:
            raise ValueError("task misconfigured: no channels")

        tg = TelegramAPI()
        try:
            checks = await tg.batch_is_user_subscribed(user_id=int(user_id), chats=required)
        except Exception as e:
            logger.warning("tasks_service: TelegramAPI failure for user=%s task=%s: %s", user_id, task_code, e)
            raise ValueError("telegram_api_unavailable")

        not_sub = [c.chat for c in checks if not c.subscribed]
        if not_sub:
            raise ValueError(f"user is not subscribed to: {', '.join(not_sub)}")

        # Успех → идём к начислению
        result = await _credit_bonus_and_finalize(
            db,
            user_id=int(user_id),
            task_code=task_code,
            reward=reward,
            idempotency_key=idk,
            cooldown_sec=cooldown_sec,
        )
        return result

    elif t_type == "watch_ad":
        # Безопасная постановка в ожидание верификации провайдера (Adsgram и др.)
        await db.execute(
            _SQL_UPSERT_USER_TASK_STATUS,
            {
                "uid": int(user_id),
                "code": task_code,
                "status": "pending_check",
                "lca": last_completed_at,
                "naa": None,  # cooldown начнётся после фактического начисления
            },
        )
        await db.commit()
        return {
            "ok": True,
            "task_code": task_code,
            "reward_bonus_efhc": Decimal("0"),
            "user_bonus_balance": None,
            "replayed": False,
        }

    else:
        raise ValueError("unsupported task type")

# -----------------------------------------------------------------------------
# Публичное API — завершение рекламной сессии по колбэку провайдера
# -----------------------------------------------------------------------------

async def confirm_ad_view_and_credit(
    db: AsyncSession,
    *,
    user_id: int,
    task_code: str,
    provider: str,
    provider_ref: str,
    reward_bonus_efhc: Decimal,
    idempotency_key: str,
) -> Dict[str, Any]:
    """
    Идемпотентное начисление бонуса за просмотр рекламы (по колбэку).
    Вызывается из интеграционного роутера (ads_routes.py) ПОСЛЕ верификации подписи провайдера.
    """
    if not idempotency_key or not idempotency_key.strip():
        raise ValueError("idempotency_key is required")
    idk = idempotency_key.strip()

    # 0) Регистрируем ключ — если уже был кредит, вернём replay
    try:
        await db.begin()
        await db.execute(_SQL_LOCK_UPSERT, {"idk": idk, "uid": int(user_id), "code": task_code})
        row = await db.execute(_SQL_LOCK_SELECT_FOR_UPDATE, {"idk": idk})
        lock = row.fetchone()
        if not lock:
            await db.rollback()
            raise RuntimeError("failed to acquire tasks lock (race)")
        _, _, _, credited = lock
        if credited:
            await db.commit()
            prev = (await db.execute(_SQL_SELECT_REWARD_LOG_BY_IDK, {"idk": idk})).fetchone()
            if prev:
                return {
                    "ok": True,
                    "task_code": str(prev[3]),
                    "reward_bonus_efhc": d8(prev[4] or 0),
                    "user_bonus_balance": None,
                    "replayed": True,
                }
            return {
                "ok": True,
                "task_code": task_code,
                "reward_bonus_efhc": Decimal("0"),
                "user_bonus_balance": None,
                "replayed": True,
            }
        await db.commit()
    except Exception:
        try:
            await db.rollback()
        except Exception:
            pass
        raise

    # 1) Читаем задание, убеждаемся, что это watch_ad, подтягиваем cooldown
    task_row = (await db.execute(_SQL_GET_TASK, {"code": task_code})).fetchone()
    if not task_row:
        raise ValueError("task not found")
    if not bool(task_row[5]):
        raise ValueError("task is disabled")

    t_type = str(task_row[7] or "").strip()
    if t_type != "watch_ad":
        raise ValueError("task type mismatch for ad confirmation")

    cooldown_sec = int(task_row[6] or 0)

    # 2) Определяем награду: доверяем провайдеру, но жёстко квантуем d8
    reward = d8(reward_bonus_efhc or 0)

    logger.info(
        "tasks_service: confirm_ad_view user=%s task=%s provider=%s ref=%s reward=%s",
        user_id, task_code, provider, provider_ref, reward,
    )

    # 3) Начисляем и финализируем (cooldown берём из tasks.cooldown_sec)
    result = await _credit_bonus_and_finalize(
        db,
        user_id=int(user_id),
        task_code=task_code,
        reward=reward,
        idempotency_key=idk,
        cooldown_sec=cooldown_sec,
    )
    return result

# -----------------------------------------------------------------------------
# Публичное API сервиса — история наград
# -----------------------------------------------------------------------------

async def list_task_rewards_log(
    db: AsyncSession,
    *,
    user_id: int,
    limit: int = 50,
    after_id: Optional[int] = None,
) -> TaskRewardsLogPage:
    lim = max(1, min(200, int(limit)))
    rows = (await db.execute(_SQL_LIST_REWARD_LOG, {"uid": int(user_id), "after_id": after_id, "lim": lim})).fetchall()
    items: List[TaskRewardLogItem] = []
    last_id = None
    for r in rows:
        rid, ts, code, amt, idk = r
        items.append(
            TaskRewardLogItem(
                id=int(rid),
                ts=ts,
                task_code=str(code),
                amount_bonus_efhc=d8(amt or 0),
                idempotency_key=str(idk),
            )
        )
        last_id = int(rid)
    next_after_id = None if len(rows) < lim else last_id
    return TaskRewardsLogPage(items=items, next_after_id=next_after_id)

# -----------------------------------------------------------------------------
# Внутреннее: единый путь начисления бонуса и финализации статусов
# -----------------------------------------------------------------------------

async def _credit_bonus_and_finalize(
    db: AsyncSession,
    *,
    user_id: int,
    task_code: str,
    reward: Decimal,
    idempotency_key: str,
    cooldown_sec: int,
) -> Dict[str, Any]:
    """
    Единый безопасный путь:
      1) Кредит из Банка в bonus_balance (idempotent).
      2) Лог task_rewards_log (UNIQUE idempotency_key).
      3) user_tasks_status → done/cooldown.
      4) Помечаем task_complete_lock.credited = TRUE.
    """
    reward_q = d8(reward or 0)

    # 1) Кредитуем бонусы из Банка (в bonus_balance)
    await credit_user_bonus_from_bank(
        db=db,
        user_id=int(user_id),
        amount=reward_q,
        reason=f"task_reward:{task_code}",
        idempotency_key=idempotency_key,
        meta={"task_code": task_code},
    )

    # 2) Лог награды
    await db.execute(
        _SQL_INSERT_REWARD_LOG,
        {"uid": int(user_id), "code": task_code, "amt": str(reward_q), "idk": idempotency_key},
    )

    # 3) Обновляем персональный статус по заданию
    next_av = None
    if cooldown_sec and cooldown_sec > 0:
        next_av = _now_utc() + timedelta(seconds=int(cooldown_sec))
        status = "cooldown"
    else:
        status = "done"

    now = _now_utc()
    await db.execute(
        _SQL_UPSERT_USER_TASK_STATUS,
        {
            "uid": int(user_id),
            "code": task_code,
            "status": status,
            "lca": now,
            "naa": next_av,
        },
    )

    # 4) Помечаем lock как credited
    await db.execute(_SQL_LOCK_SET_CREDITED, {"idk": idempotency_key})
    await db.commit()

    return {
        "ok": True,
        "task_code": task_code,
        "reward_bonus_efhc": reward_q,
        "user_bonus_balance": None,  # баланс не считываем тут (экономия); фронт при желании спросит профиль
        "replayed": False,
    }
