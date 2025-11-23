# -*- coding: utf-8 -*-
# backend/app/services/energy_service.py
# =============================================================================
# EFHC Bot — Сервис генерации энергии (только посекундные ставки, ИИ-самовосстановление)
# -----------------------------------------------------------------------------
# Назначение:
#   • Начисляет энергию (kWh) по активным панелям, строго используя ТОЛЬКО
#     посекундные константы из конфигурации:
#       GEN_PER_SEC_BASE_KWH (без VIP)
#       GEN_PER_SEC_VIP_KWH  (VIP)
#   • Поддерживает два сценария:
#       1) Пакетная генерация для всего пула панелей (фоновый планировщик).
#       2) Точный «догон» генерации пользователя при открытии его экрана.
#
# Принципы и инварианты (канон):
#   • Никаких «суточных» значений — только per-second ставки из конфига.
#   • Балансы энергии храним раздельно:
#       users.available_kwh       — доступно к обмену,
#       users.total_generated_kwh — для рейтинга/достижений.
#   • Идемпотентность начислений обеспечивается полем panels.last_generated_at:
#       начисляем Δt = min(now, expires_at) - last_generated_at; затем
#       last_generated_at := min(now, expires_at). Повтор не даёт дублей.
#   • Безопасная конкуренция: выбор панелей под UPDATE ... SKIP LOCKED.
#     Это позволяет нескольким воркерам работать параллельно без конфликтов.
#   • Округление вниз до 8 знаков — единая утилита deps.d8 (Decimal(30,8)).
#   • Пользовательские денежные балансы не затрагиваем; здесь только kWh.
#
# ИИ-самовосстановление:
#   • Пакетная обработка с «мягкими» ретраями и fallback на поштучную обработку.
#   • Любая нештатная ситуация по панели не валит цикл — логируем и идём дальше.
#   • Повторный запуск обработает «хвосты» автоматически (по last_generated_at).
#
# Таблицы (минимум):
#   {SCHEMA}.users(
#       telegram_id,
#       is_vip,
#       available_kwh,
#       total_generated_kwh,
#       ...
#   )
#   {SCHEMA}.panels(
#       id,
#       user_id,               -- ТГ ID пользователя (связь по users.telegram_id)
#       is_active,
#       last_generated_at,
#       expires_at,
#       base_gen_per_sec,      -- может быть, но ставки берём из конфига
#       generated_kwh,
#       ...
#   )
#
# Публичные функции:
#   • generate_energy_tick(db, batch_size=500) -> dict
#   • generate_energy_for_user(db, user_id) -> dict
#   • backfill_all(db, max_rounds=50, batch_size=500) -> dict
# =============================================================================

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config_core import get_settings
from backend.app.core.logging_core import get_logger
from backend.app.deps import d8  # единое округление до 8 знаков, ROUND_DOWN

logger = get_logger(__name__)
settings = get_settings()
SCHEMA = getattr(settings, "DB_SCHEMA_CORE", "efhc_core")

# -----------------------------------------------------------------------------
# ЕДИНЫЙ источник истинных ставок — только из конфигурации
#   • никаких «локальных копий» значений в коде;
#   • при смене GEN_PER_SEC_* в .env сервис будет использовать новые ставки
#     без пересборки (при перезапуске приложения).
# -----------------------------------------------------------------------------

def _rate_base() -> Decimal:
    """
    Базовая ставка генерации (без VIP), kWh/сек, из настроек.
    """
    return d8(getattr(settings, "GEN_PER_SEC_BASE_KWH", "0.00000692") or "0.00000692")


def _rate_vip() -> Decimal:
    """
    VIP-ставка генерации, kWh/сек, из настроек.
    """
    return d8(getattr(settings, "GEN_PER_SEC_VIP_KWH", "0.00000741") or "0.00000741")


# -----------------------------------------------------------------------------
# SQL-фрагменты (намеренно простые и прозрачные)
# -----------------------------------------------------------------------------

# Выборка пачки активных панелей, у которых есть что догенерировать,
# c блокировкой и пропуском занятых строк (SKIP LOCKED).
_SELECT_PANELS_BATCH_SQL = text(
    f"""
    SELECT p.id,
           p.user_id,
           p.last_generated_at,
           p.expires_at,
           u.is_vip
      FROM {SCHEMA}.panels AS p
      JOIN {SCHEMA}.users  AS u ON u.telegram_id = p.user_id
     WHERE p.is_active = TRUE
       AND p.last_generated_at < LEAST(NOW(), p.expires_at)
     ORDER BY p.last_generated_at ASC
     FOR UPDATE SKIP LOCKED
     LIMIT :lim
    """
)

# Обновление панели (фиксируем сдвиг last_generated_at и прирост по самой панели)
_UPDATE_PANEL_ONE_SQL = text(
    f"""
    UPDATE {SCHEMA}.panels
       SET last_generated_at = :new_ts,
           generated_kwh     = generated_kwh + :add_kwh
     WHERE id = :pid
    """
)

# Пакетное обновление пользовательской энергии (по накопленным суммам)
_UPDATE_USER_ENERGY_SQL = text(
    f"""
    UPDATE {SCHEMA}.users
       SET available_kwh       = COALESCE(available_kwh, 0) + :add_kwh,
           total_generated_kwh = COALESCE(total_generated_kwh, 0) + :add_kwh,
           updated_at          = NOW()
     WHERE telegram_id = :uid
    """
)

# Выбор всех активных панелей конкретного пользователя под «догон»
_SELECT_USER_PANELS_SQL = text(
    f"""
    SELECT p.id, p.last_generated_at, p.expires_at, u.is_vip
      FROM {SCHEMA}.panels p
      JOIN {SCHEMA}.users  u ON u.telegram_id = p.user_id
     WHERE p.user_id = :uid
       AND p.is_active = TRUE
       AND p.last_generated_at < LEAST(NOW(), p.expires_at)
     ORDER BY p.last_generated_at ASC
     FOR UPDATE SKIP LOCKED
    """
)


# -----------------------------------------------------------------------------
# Вспомогательные расчёты
# -----------------------------------------------------------------------------

def _now_utc() -> datetime:
    return datetime.now(tz=timezone.utc)


def _calc_add_kwh(delta_sec: int, is_vip: bool) -> Decimal:
    """
    Возвращает ΔkWh = delta_sec * rate, округляя вниз до 8 знаков.

    ИИ-защита:
      • Для отрицательных/нулевых интервалов всегда возвращаем 0.
      • Ставка берётся только из конфигурации (_rate_base/_rate_vip).
    """
    if delta_sec <= 0:
        return d8(0)
    rate = _rate_vip() if is_vip else _rate_base()
    add = Decimal(delta_sec) * rate
    return d8(add)


def _clamp_to_expiry(ts_last: datetime, ts_expire: datetime, ts_now: datetime) -> Tuple[datetime, int]:
    """
    Считает новый момент last_generated_at' = min(now, expires_at) и количество секунд Δ.

    Возвращает (new_last, delta_seconds).
    ИИ-защита:
      • Если по каким-то причинам end_ts < ts_last → delta=0 (ничего не начисляем).
    """
    end_ts = ts_now if ts_now <= ts_expire else ts_expire
    delta = int((end_ts - ts_last).total_seconds())
    if delta < 0:
        delta = 0
    return (end_ts, delta)


# -----------------------------------------------------------------------------
# Пакетная генерация (фоновый планировщик)
# -----------------------------------------------------------------------------

async def generate_energy_tick(db: AsyncSession, *, batch_size: int = 500) -> Dict[str, Any]:
    """
    Обрабатывает до batch_size панелей, которым есть что догенерировать.

    Стратегия:
      1) В рамках ОДНОЙ транзакции выбираем партию панелей (FOR UPDATE SKIP LOCKED).
      2) Для каждой панели считаем Δсекунд, ΔkWh и обновляем panel + накапливаем сумму по пользователю.
      3) После цикла — в той же транзакции делаем UPDATE по каждому пользователю
         (+available_kwh, +total_generated_kwh).

    ИИ-самовосстановление:
      • Если пакетная транзакция упала (например, deadlock/serialize), мы откатываем её
        и выполняем fallback: поштучная обработка проблемных панелей
        (каждая в своей мини-транзакции, устойчива к конфликтам).

    Возвращает статистику:
      {
        "panels_processed": int,
        "users_updated": int,
        "total_kwh_added": str(Decimal),
        "partial_fallbacks": int,
      }
    """
    stats = {
        "panels_processed": 0,
        "users_updated": 0,
        "total_kwh_added": str(d8(0)),
        "partial_fallbacks": 0,
    }

    if batch_size < 1:
        batch_size = 1

    # 1) Пакетная попытка в одной транзакции (правильная конкуренция с SKIP LOCKED)
    users_acc: Dict[int, Decimal] = {}
    total_add = d8(0)
    items: List[Tuple[Any, ...]] = []

    try:
        await db.begin()

        # Выбираем партию под блокировкой
        row = await db.execute(_SELECT_PANELS_BATCH_SQL, {"lim": int(batch_size)})
        items = row.fetchall()
        if not items:
            await db.commit()
            return stats

        now_ts = _now_utc()

        # Обрабатываем панели
        for (pid, uid, last_ts, exp_ts, is_vip) in items:
            try:
                new_last, delta_sec = _clamp_to_expiry(last_ts, exp_ts, now_ts)
                if delta_sec <= 0:
                    # уже догенерирована до текущего момента / истечения
                    continue

                add_kwh = _calc_add_kwh(delta_sec, bool(is_vip))
                if add_kwh < 0:
                    # защитный «предохранитель» от аномалий
                    logger.warning(
                        "energy.tick: negative add_kwh=%s for panel=%s, user=%s; forcing 0",
                        add_kwh,
                        pid,
                        uid,
                    )
                    add_kwh = d8(0)

                # Обновляем панель
                await db.execute(
                    _UPDATE_PANEL_ONE_SQL,
                    {
                        "new_ts": new_last,
                        "add_kwh": str(add_kwh),
                        "pid": int(pid),
                    },
                )

                # Накапливаем по пользователю
                if add_kwh > 0:
                    users_acc[uid] = d8(users_acc.get(uid, d8(0)) + add_kwh)
                    total_add = d8(total_add + add_kwh)
                    stats["panels_processed"] += 1

            except Exception as pe:
                # Не валим весь пакет — панель будет обработана fallback-логикой
                logger.debug("energy.tick: panel %s batch-step failed: %s", pid, pe)
                continue

        # Обновляем пользователей суммарно
        updated_users = 0
        for uid, add_sum in users_acc.items():
            if add_sum <= 0:
                continue
            await db.execute(
                _UPDATE_USER_ENERGY_SQL,
                {
                    "add_kwh": str(add_sum),
                    "uid": int(uid),
                },
            )
            updated_users += 1

        await db.commit()
        stats["users_updated"] = updated_users
        stats["total_kwh_added"] = str(total_add)

    except Exception as e:
        # Пакет не удался — откатываем и переходим к поштучной обработке (fallback)
        try:
            await db.rollback()
        except Exception:
            pass
        logger.warning("energy.tick: batch failed → fallback: %s", e)
        stats["partial_fallbacks"] += 1

        # 2) Fallback: по одной панели, каждая в своей транзакции (устойчиво к конфликтам)
        total_add = d8(0)
        stats["panels_processed"] = 0
        stats["users_updated"] = 0

        for (pid, uid, last_ts, exp_ts, is_vip) in items or []:
            try:
                await db.begin()
                # заново читаем актуальные значения (могли измениться)
                row = await db.execute(
                    text(
                        f"""
                        SELECT p.id, p.user_id, p.last_generated_at, p.expires_at, u.is_vip
                          FROM {SCHEMA}.panels p
                          JOIN {SCHEMA}.users  u ON u.telegram_id = p.user_id
                         WHERE p.id = :pid
                           AND p.is_active = TRUE
                           AND p.last_generated_at < LEAST(NOW(), p.expires_at)
                         FOR UPDATE SKIP LOCKED
                        """
                    ),
                    {"pid": int(pid)},
                )
                one = row.fetchone()
                if not one:
                    await db.rollback()
                    continue

                _, uid2, last2, exp2, vip2 = one
                now2 = _now_utc()
                new_last2, delta2 = _clamp_to_expiry(last2, exp2, now2)
                if delta2 <= 0:
                    await db.rollback()
                    continue

                add2 = _calc_add_kwh(delta2, bool(vip2))
                if add2 < 0:
                    logger.warning(
                        "energy.tick.fallback: negative add_kwh=%s for panel=%s, user=%s; forcing 0",
                        add2,
                        pid,
                        uid2,
                    )
                    add2 = d8(0)

                await db.execute(
                    _UPDATE_PANEL_ONE_SQL,
                    {"new_ts": new_last2, "add_kwh": str(add2), "pid": int(pid)},
                )
                if add2 > 0:
                    await db.execute(
                        _UPDATE_USER_ENERGY_SQL,
                        {"add_kwh": str(add2), "uid": int(uid2)},
                    )
                    total_add = d8(total_add + add2)
                    stats["panels_processed"] += 1
                    # users_updated — приблизительный счётчик, возможны дубли по одному uid
                    stats["users_updated"] += 1
                await db.commit()
            except Exception as e2:
                try:
                    await db.rollback()
                except Exception:
                    pass
                logger.error("energy.tick: fallback panel %s failed: %s", pid, e2)

        stats["total_kwh_added"] = str(total_add)

    return stats


# -----------------------------------------------------------------------------
# Догон для одного пользователя (при открытии его экрана)
# -----------------------------------------------------------------------------

async def generate_energy_for_user(db: AsyncSession, *, user_id: int) -> Dict[str, Any]:
    """
    Начисляет энергию по всем активным панелям пользователя «здесь и сейчас».

    Типичный сценарий:
      • вызывается при открытии пользователем разделов Dashboard / Panels / Exchange;
      • гарантирует, что available_kwh и total_generated_kwh не «отстают» от панелей
        дольше, чем до ближайшего открытия экрана.

    ИИ-защита:
      • Сначала пробуем пакетно (все панели пользователя в одной транзакции).
      • При любой ошибке переходим к поштучному fallback для каждой панели этого пользователя.
    """
    now_ts = _now_utc()
    total_add = d8(0)
    panels = 0
    items: List[Tuple[Any, ...]] = []

    try:
        await db.begin()
        row = await db.execute(_SELECT_USER_PANELS_SQL, {"uid": int(user_id)})
        items = row.fetchall()
        if not items:
            await db.commit()
            return {"user_id": int(user_id), "panels_processed": 0, "kwh_added": str(d8(0))}

        add_sum_user = d8(0)

        for (pid, last_ts, exp_ts, is_vip) in items:
            new_last, delta_sec = _clamp_to_expiry(last_ts, exp_ts, now_ts)
            if delta_sec <= 0:
                continue
            add_kwh = _calc_add_kwh(delta_sec, bool(is_vip))
            if add_kwh < 0:
                logger.warning(
                    "energy.user(%s): negative add_kwh=%s for panel=%s; forcing 0",
                    user_id,
                    add_kwh,
                    pid,
                )
                add_kwh = d8(0)

            await db.execute(
                _UPDATE_PANEL_ONE_SQL,
                {"new_ts": new_last, "add_kwh": str(add_kwh), "pid": int(pid)},
            )
            add_sum_user = d8(add_sum_user + add_kwh)
            total_add = d8(total_add + add_kwh)
            panels += 1

        if add_sum_user > 0:
            await db.execute(
                _UPDATE_USER_ENERGY_SQL,
                {"add_kwh": str(add_sum_user), "uid": int(user_id)},
            )

        await db.commit()
    except Exception as e:
        try:
            await db.rollback()
        except Exception:
            pass
        # Мягкий fallback: попробуем поштучно
        logger.warning("energy.user(%s): batch failed → fallback: %s", user_id, e)
        total_add = d8(0)
        panels = 0

        for (pid, last_ts, exp_ts, is_vip) in items or []:
            try:
                await db.begin()
                row2 = await db.execute(
                    text(
                        f"""
                        SELECT p.id, p.last_generated_at, p.expires_at, u.is_vip
                          FROM {SCHEMA}.panels p
                          JOIN {SCHEMA}.users  u ON u.telegram_id = p.user_id
                         WHERE p.id = :pid
                           AND p.user_id = :uid
                           AND p.is_active = TRUE
                           AND p.last_generated_at < LEAST(NOW(), p.expires_at)
                         FOR UPDATE SKIP LOCKED
                        """
                    ),
                    {"pid": int(pid), "uid": int(user_id)},
                )
                one = row2.fetchone()
                if not one:
                    await db.rollback()
                    continue
                _, last2, exp2, vip2 = one
                now2 = _now_utc()
                new_last2, delta2 = _clamp_to_expiry(last2, exp2, now2)
                if delta2 <= 0:
                    await db.rollback()
                    continue
                add2 = _calc_add_kwh(delta2, bool(vip2))
                if add2 < 0:
                    logger.warning(
                        "energy.user(%s).fallback: negative add_kwh=%s for panel=%s; forcing 0",
                        user_id,
                        add2,
                        pid,
                    )
                    add2 = d8(0)
                await db.execute(
                    _UPDATE_PANEL_ONE_SQL,
                    {"new_ts": new_last2, "add_kwh": str(add2), "pid": int(pid)},
                )
                if add2 > 0:
                    await db.execute(
                        _UPDATE_USER_ENERGY_SQL,
                        {"add_kwh": str(add2), "uid": int(user_id)},
                    )
                    total_add = d8(total_add + add2)
                    panels += 1
                await db.commit()
            except Exception as e2:
                try:
                    await db.rollback()
                except Exception:
                    pass
                logger.error("energy.user(%s): fallback panel %s failed: %s", user_id, pid, e2)

    return {
        "user_id": int(user_id),
        "panels_processed": int(panels),
        "kwh_added": str(total_add),
    }


# -----------------------------------------------------------------------------
# Полная «догонка» всего пула (для запуска после простоев)
# -----------------------------------------------------------------------------

async def backfill_all(
    db: AsyncSession,
    *,
    max_rounds: int = 50,
    batch_size: int = 500,
) -> Dict[str, Any]:
    """
    Многократный вызов generate_energy_tick, пока есть необработанные панели
    или не достигнут предел раундов.

    Использование:
      • после длительного простоя,
      • после миграций/обновлений, когда last_generated_at могли «отстать».

    ИИ-самовосстановление:
      • Каждый раунд идемпотентен по last_generated_at/expires_at.
      • Даже при падениях/рестартах суммарная генерация корректна.
    """
    if max_rounds < 1:
        max_rounds = 1

    total_panels = 0
    total_users_updates = 0
    total_kwh = d8(0)
    rounds = 0
    partials = 0

    while rounds < max_rounds:
        rounds += 1
        res = await generate_energy_tick(db, batch_size=batch_size)
        total_panels += int(res.get("panels_processed", 0))
        total_users_updates += int(res.get("users_updated", 0))
        total_kwh = d8(total_kwh + Decimal(res.get("total_kwh_added", "0")))
        partials += int(res.get("partial_fallbacks", 0))

        # Если в этом раунде уже нечего было обрабатывать — выходим
        if int(res.get("panels_processed", 0)) == 0:
            break

        # Короткий не блокирующий «вздох» между раундами
        await asyncio.sleep(0.05)

    return {
        "rounds": rounds,
        "panels_processed": total_panels,
        "users_updated": total_users_updates,
        "total_kwh_added": str(total_kwh),
        "fallback_rounds": partials,
    }


# =============================================================================
# Пояснения «для чайника»:
#   • Почему нет «суточных» значений?
#     — Канон прямо запрещает: единственный источник — ставки в секундах из .env.
#   • Почему не считаем «помесячно/по дням»?
#     — Потому что это даёт двусмысленность и накопленные ошибки округления.
#       last_generated_at + pos/сек — всегда однозначно и идемпотентно.
#   • Почему SKIP LOCKED?
#     — Чтобы несколько воркеров-планировщиков могли безопасно работать параллельно
#       без взаимного блокирования (каждый «забирает» свою порцию панелей).
#   • Почему два поля у пользователя?
#     — available_kwh для обмена; total_generated_kwh для рейтинга/достижений.
#   • Что если сервер упал посреди пакета?
#     — При новом запуске last_generated_at «скажет», сколько ещё нужно догенерировать.
# =============================================================================

__all__ = [
    "generate_energy_tick",
    "generate_energy_for_user",
    "backfill_all",
]
