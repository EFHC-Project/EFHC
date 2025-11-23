# -*- coding: utf-8 -*-
# backend/app/services/referral_service.py
# =============================================================================
# Назначение кода:
#   Сервис реферальной системы EFHC Bot:
#   • Курсорные выборки «Активные / Неактивные» рефералы для пригласившего.
#   • Регистрация реферальной связи при старте бота по ссылке (БЕЗ бонуса).
#   • Автоматическое начисление 0.1 EFHC за КАЖДОГО АКТИВНОГО реферала
#     (купил ≥1 панель), но только для первых 10 000 активных рефералов.
#   • Ранги (уровни 0–5) по количеству АКТИВНЫХ рефералов + разовые бонусы
#     за достижение уровней и прогресс до следующего уровня для фронтенда.
#   • Генерация постоянного реферального кода/ссылки для пользователя.
#
# Канон/инварианты:
#   • «Активный реферал» — тот, кто купил хотя бы одну панель (статус постоянный).
#   • Бонусы:
#       • За КАЖДОГО активного реферала:
#           +0.1 EFHC (bonus balance) пригласившему, но максимум для первых
#           10 000 активных рефералов.
#       • Уровни:
#           1 уровень: 10 активных рефералов   → +1 EFHC
#           2 уровень: 100 активных рефералов  → +10 EFHC
#           3 уровень: 1000 активных рефералов → +100 EFHC
#           4 уровень: 3000 активных рефералов → +300 EFHC
#           5 уровень: 10000 активных рефералов→ +1000 EFHC
#
#   • Любые начисления идут ТОЛЬКО через банк (transactions_service),
#     Decimal(30,8), округление вниз, bonus-first.
#   • Пользователь НЕ может уйти в минус; Банк МОЖЕТ (операции не блокируем).
#   • Денежные операции — строго идемпотентны (Idempotency-Key на уровне банка).
#
# ИИ-защита/самовосстановление:
#   • Курсорная пагинация без OFFSET: (invited_at, referred_telegram_id) как курсор.
#   • «Мягкие» SQL: сервис использует текстовые запросы, легко адаптируются миграциями.
#   • on_user_first_panel_purchase(...) построен по принципу read-through:
#     повторный вызов с теми же ключами безопасен (idempotency_key).
#   • Ранги начисляются отдельным helper’ом с идемпотентными ключами на уровень.
#
# Запреты:
#   • Нет P2P-переводов; никаких прямых операций user→user.
#   • Нет «суточных» расчётов; сервис не знает про генерацию энергии.
# =============================================================================

from __future__ import annotations

import base64
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config_core import get_settings
from backend.app.core.logging_core import get_logger
from backend.app.deps import d8  # централизованное округление Decimal(8)
from backend.app.services.transactions_service import (
    credit_user_bonus_from_bank,  # Бонус идёт из Банка (bonus balance)
)

logger = get_logger(__name__)
settings = get_settings()
SCHEMA = getattr(settings, "DB_SCHEMA_CORE", "efhc_core")

# -----------------------------------------------------------------------------
# Константы реферальной системы
# -----------------------------------------------------------------------------

# 0.1 EFHC за КАЖДОГО АКТИВНОГО реферала (после покупки панели)
REF_BONUS_PER_ACTIVE_UNIT_RAW = getattr(settings, "REFERRAL_BONUS_PER_ACTIVE_UNIT", "0.1") or "0.1"
REF_BONUS_PER_ACTIVE_UNIT = d8(REF_BONUS_PER_ACTIVE_UNIT_RAW)

# Ограничение количества АКТИВНЫХ рефералов с бонусом 0.1 EFHC
REF_MAX_ACTIVE_FOR_UNIT_BONUS: int = int(
    getattr(settings, "REFERRAL_MAX_ACTIVE_FOR_UNIT_BONUS", 10_000) or 10_000
)

# Конфигурация уровней 1–5 (по ЧИСЛУ АКТИВНЫХ рефералов)
@dataclass(frozen=True)
class ReferralLevelConfig:
    level: int
    required_active: int
    bonus: Decimal  # EFHC, начисляется разово при достижении уровня


REF_LEVELS: Tuple[ReferralLevelConfig, ...] = (
    ReferralLevelConfig(level=1, required_active=10,    bonus=d8("1")),
    ReferralLevelConfig(level=2, required_active=100,   bonus=d8("10")),
    ReferralLevelConfig(level=3, required_active=1000,  bonus=d8("100")),
    ReferralLevelConfig(level=4, required_active=3000,  bonus=d8("300")),
    ReferralLevelConfig(level=5, required_active=10000, bonus=d8("1000")),
)

# Настройки реферального кода/ссылки
REF_CODE_LENGTH: int = int(getattr(settings, "REFERRAL_CODE_LENGTH", 8) or 8)
REF_CODE_ALPHABET: str = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # без похожих символов
REF_SALT: str = getattr(settings, "REFERRAL_SALT", "efhc_ref_salt") or "efhc_ref_salt"
BOT_USERNAME: str = getattr(settings, "BOT_USERNAME", "") or ""

# -----------------------------------------------------------------------------
# DTO курсорных ответов и статистики
# -----------------------------------------------------------------------------

@dataclass
class ReferralItem:
    telegram_id: int
    username: Optional[str]
    is_active: bool
    panels_count: int
    invited_at: datetime
    activated_at: Optional[datetime]


CursorTuple = Tuple[str, int]  # (created_at_iso, referred_telegram_id)


@dataclass
class ReferralStats:
    inviter_telegram_id: int
    total_referrals: int          # все рефералы (по ссылке)
    active_referrals: int         # активные (купили ≥ 1 панель)
    level: int                    # 0..5
    next_level: Optional[int]     # None, если достигнут 5 уровень
    current_level_min: int        # сколько активных нужно для текущего уровня (0 для уровня 0)
    next_level_min: Optional[int] # сколько активных нужно для следующего уровня
    refs_to_next: int             # сколько активных не хватает до следующего уровня (0, если max)
    progress: float               # 0.0–1.0 прогресс от текущего уровня до следующего
    referral_code: str            # стабильный код
    referral_link: Optional[str]  # t.me-ссылка, если BOT_USERNAME задан


# -----------------------------------------------------------------------------
# Внутренние SQL-хелперы для списков рефералов
# -----------------------------------------------------------------------------

# Таблицы по канону:
#   • {SCHEMA}.referral_links(
#         inviter_telegram_id BIGINT,
#         referred_telegram_id BIGINT,
#         created_at TIMESTAMPTZ,
#         activated_at TIMESTAMPTZ NULL
#     )
#   • {SCHEMA}.users(id PK, telegram_id BIGINT, username TEXT, ...)
#   • {SCHEMA}.panels(id PK, user_id FK users.id, is_active BOOL, ...)
#
# Активность считаем исторически: «активный» = есть хотя бы одна панель (в т.ч. архивная).
SQL_BASE = f"""
WITH ref AS (
  SELECT
    rl.referred_telegram_id AS ref_tg,
    rl.inviter_telegram_id  AS inv_tg,
    rl.created_at           AS invited_at,
    rl.activated_at         AS activated_at
  FROM {SCHEMA}.referral_links rl
  WHERE rl.inviter_telegram_id = :inviter_tg
),
u AS (
  SELECT
    u.telegram_id AS tg,
    u.username    AS username,
    u.id          AS user_id
  FROM {SCHEMA}.users u
  WHERE u.telegram_id IN (SELECT ref_tg FROM ref)
),
p AS (
  SELECT
    u.tg AS tg,
    COUNT(p.id) AS panels_count
  FROM u
  LEFT JOIN {SCHEMA}.panels p ON p.user_id = u.user_id
  GROUP BY u.tg
)
SELECT
  ref.ref_tg            AS telegram_id,
  u.username            AS username,
  (COALESCE(p.panels_count,0) > 0) AS is_active,
  COALESCE(p.panels_count,0) AS panels_count,
  ref.invited_at        AS invited_at,
  ref.activated_at      AS activated_at
FROM ref
LEFT JOIN u  ON u.tg = ref.ref_tg
LEFT JOIN p  ON p.tg = ref.ref_tg
WHERE
  (ref.invited_at, ref.ref_tg) < (:cursor_ts, :cursor_id)
  OR (:cursor_ts IS NULL AND :cursor_id IS NULL)
  -- фильтр активности подставляется снаружи
ORDER BY ref.invited_at DESC, ref.ref_tg DESC
LIMIT :limit
"""

# -----------------------------------------------------------------------------
# Публичные функции: списки рефералов для фронтенда
# -----------------------------------------------------------------------------

async def svc_list_referrals_active(
    db: AsyncSession,
    inviter_telegram_id: int,
    limit: int,
    cursor: Optional[CursorTuple],
) -> Tuple[List[ReferralItem], Optional[CursorTuple]]:
    """
    Курсорная выдача «Активных рефералов» (купили ≥1 панель, исторический статус).
    Cursor: (invited_at_iso, referred_telegram_id) в порядке DESC.
    """
    cur_ts, cur_id = _normalize_cursor(cursor)

    sql = SQL_BASE + "\nAND (COALESCE(p.panels_count,0) > 0)"
    rows = await db.execute(
        text(sql),
        {
            "inviter_tg": int(inviter_telegram_id),
            "cursor_ts": cur_ts,
            "cursor_id": cur_id,
            "limit": int(limit),
        },
    )
    items = [
        ReferralItem(
            telegram_id=int(r[0]),
            username=(r[1] if r[1] is not None else None),
            is_active=bool(r[2]),
            panels_count=int(r[3]),
            invited_at=r[4],
            activated_at=r[5],
        )
        for r in rows.fetchall()
    ]
    next_cur = _next_cursor(items)
    return items, next_cur


async def svc_list_referrals_inactive(
    db: AsyncSession,
    inviter_telegram_id: int,
    limit: int,
    cursor: Optional[CursorTuple],
) -> Tuple[List[ReferralItem], Optional[CursorTuple]]:
    """
    Курсорная выдача «Неактивных рефералов» (не купили ни одной панели).
    Cursor: (invited_at_iso, referred_telegram_id) в порядке DESC.
    """
    cur_ts, cur_id = _normalize_cursor(cursor)

    sql = SQL_BASE + "\nAND (COALESCE(p.panels_count,0) = 0)"
    rows = await db.execute(
        text(sql),
        {
            "inviter_tg": int(inviter_telegram_id),
            "cursor_ts": cur_ts,
            "cursor_id": cur_id,
            "limit": int(limit),
        },
    )
    items = [
        ReferralItem(
            telegram_id=int(r[0]),
            username=(r[1] if r[1] is not None else None),
            is_active=bool(r[2]),
            panels_count=int(r[3]),
            invited_at=r[4],
            activated_at=r[5],
        )
        for r in rows.fetchall()
    ]
    next_cur = _next_cursor(items)
    return items, next_cur


# -----------------------------------------------------------------------------
# Регистрация нового реферала по ссылке (БЕЗ бонуса)
#   Вызывается из регистрации пользователя, если в /start есть реф-код.
# -----------------------------------------------------------------------------

async def register_referral_and_reward(
    db: AsyncSession,
    *,
    inviter_telegram_id: int,
    referred_telegram_id: int,
    created_at: Optional[datetime] = None,
) -> None:
    """
    Регистрирует запись в referral_links.
    Бонусов за регистрацию НЕТ (по канону). Бонусы только за АКТИВНЫХ пользователей,
    то есть после покупки хотя бы одной панели (см. on_user_first_panel_purchase).
    """
    created_at = created_at or datetime.now(tz=timezone.utc)

    await db.execute(
        text(
            f"""
            INSERT INTO {SCHEMA}.referral_links (inviter_telegram_id, referred_telegram_id, created_at)
            VALUES (:inv, :ref, :ts)
            ON CONFLICT DO NOTHING
            """
        ),
        {"inv": int(inviter_telegram_id), "ref": int(referred_telegram_id), "ts": created_at},
    )
    # никаких денежных операций — бонус начисляется только при активации (см. ниже)


# -----------------------------------------------------------------------------
# Авто-активация и бонусы при первой покупке панели приглашённым
#   Вызывается panels_service после успешной покупки ПЕРВОЙ панели.
# -----------------------------------------------------------------------------

async def on_user_first_panel_purchase(
    db: AsyncSession,
    buyer_telegram_id: int,
    activation_dt: Optional[datetime] = None,
) -> None:
    """
    Помечает реферала активным (если есть запись) и пытается:
      • Начислить 0.1 EFHC за АКТИВНОГО реферала (до 10 000 активных рефералов).
      • Обновить/начислить уровеньные бонусы (1–5) по количеству АКТИВНЫХ рефералов.
    """
    act_ts = activation_dt or datetime.utcnow()

    # 1) Найти пригласившего для buyer_telegram_id
    row = await db.execute(
        text(
            f"""
            SELECT inviter_telegram_id, referred_telegram_id, created_at, activated_at
            FROM {SCHEMA}.referral_links
            WHERE referred_telegram_id = :tg
            LIMIT 1
            """
        ),
        {"tg": int(buyer_telegram_id)},
    )
    ref = row.fetchone()
    if not ref:
        # Покупатель пришёл без реф-ссылки — нечего активировать
        logger.debug("referral: no link for buyer tg=%s", buyer_telegram_id)
        return

    inviter_tg = int(ref[0])

    # 2) Проставить activated_at (если ещё пусто)
    await db.execute(
        text(
            f"""
            UPDATE {SCHEMA}.referral_links
            SET activated_at = COALESCE(activated_at, :ts)
            WHERE referred_telegram_id = :tg
            """
        ),
        {"tg": int(buyer_telegram_id), "ts": act_ts},
    )

    # 3) Начислить 0.1 EFHC за АКТИВНОГО реферала (unit-бонус)
    try:
        await _maybe_reward_active_unit_bonus(
            db=db,
            inviter_telegram_id=inviter_tg,
            buyer_telegram_id=buyer_telegram_id,
        )
    except Exception as e:
        # Не роняем покупку панели; бонус догоним ретраем
        logger.warning("referral unit bonus failed (will retry later): buyer=%s err=%s", buyer_telegram_id, e)

    # 4) Синхронизировать уровень и уровеньные бонусы (1–5) пригласившего
    try:
        await _sync_inviter_level_rewards(db=db, inviter_telegram_id=inviter_tg)
    except Exception as e:
        logger.warning("referral levels sync failed (will retry later): inviter=%s err=%s", inviter_tg, e)


# -----------------------------------------------------------------------------
# Статистика для фронтенда: уровни, прогресс, ссылка
# -----------------------------------------------------------------------------

async def get_referral_stats(
    db: AsyncSession,
    *,
    inviter_telegram_id: int,
) -> ReferralStats:
    """
    Возвращает агрегированную статистику для реферального раздела:
      • total_referrals      — общее число рефералов (по ссылке)
      • active_referrals     — активные (купили ≥1 панель)
      • level (0–5)
      • next_level / refs_to_next / progress (0.0–1.0)
      • referral_code, referral_link
    """
    total_refs = await _count_total_referrals(db, inviter_telegram_id=inviter_telegram_id)
    active_refs = await _count_active_referrals(db, inviter_telegram_id=inviter_telegram_id)

    level, current_min, next_level, next_min, refs_to_next, progress = _compute_level_and_progress(active_refs)

    code = await get_or_create_referral_code(db, inviter_telegram_id=inviter_telegram_id)
    link = build_referral_link(code) if code and BOT_USERNAME else None

    return ReferralStats(
        inviter_telegram_id=int(inviter_telegram_id),
        total_referrals=total_refs,
        active_referrals=active_refs,
        level=level,
        next_level=next_level,
        current_level_min=current_min,
        next_level_min=next_min,
        refs_to_next=refs_to_next,
        progress=progress,
        referral_code=code,
        referral_link=link,
    )


# -----------------------------------------------------------------------------
# Денежная логика: 0.1 EFHC за активного реферала и уровеньные бонусы
# -----------------------------------------------------------------------------

async def _maybe_reward_active_unit_bonus(
    db: AsyncSession,
    inviter_telegram_id: int,
    buyer_telegram_id: int,
) -> None:
    """
    Идемпотентное начисление 0.1 EFHC за АКТИВНОГО реферала.
    Условия:
      • Покупатель стал активным (первая панель).
      • Общее число АКТИВНЫХ рефералов у пригласившего ≤ REF_MAX_ACTIVE_FOR_UNIT_BONUS.
    Idempotency-Key = "REF_ACT_UNIT:<buyer_tg>" — повтор не создаст дубль.
    """
    inviter_user_id = await _get_user_id_by_telegram(db, inviter_telegram_id)
    if inviter_user_id is None:
        logger.warning("referral: inviter not found for unit bonus, tg=%s", inviter_telegram_id)
        return

    # Считаем активных рефералов после активации
    active_refs = await _count_active_referrals(db, inviter_telegram_id=inviter_telegram_id)
    if active_refs > REF_MAX_ACTIVE_FOR_UNIT_BONUS:
        logger.info(
            "referral: max active-unit bonus reached for inviter=%s (active_refs=%s)",
            inviter_telegram_id,
            active_refs,
        )
        return

    if REF_BONUS_PER_ACTIVE_UNIT <= d8("0"):
        return

    await credit_user_bonus_from_bank(
        db=db,
        user_id=int(inviter_user_id),
        amount=REF_BONUS_PER_ACTIVE_UNIT,
        reason="referral_active_unit",
        idempotency_key=f"REF_ACT_UNIT:{int(buyer_telegram_id)}",
        meta={"inviter_tg": int(inviter_telegram_id), "buyer_tg": int(buyer_telegram_id)},
    )


async def _sync_inviter_level_rewards(
    db: AsyncSession,
    inviter_telegram_id: int,
) -> None:
    """
    Начисляет ВСЕ уровеньные бонусы (1–5), на которые уже хватает АКТИВНЫХ рефералов.
    Идемпотентность достигается через стабильный ключ на каждый уровень:
      "REF_LVL:<user_id>:<level>".
    Повторный вызов безопасен.
    """
    inviter_user_id = await _get_user_id_by_telegram(db, inviter_telegram_id)
    if inviter_user_id is None:
        logger.warning("referral: inviter not found for level sync, tg=%s", inviter_telegram_id)
        return

    active_refs = await _count_active_referrals(db, inviter_telegram_id=inviter_telegram_id)
    if active_refs <= 0:
        return

    for cfg in REF_LEVELS:
        if active_refs >= cfg.required_active:
            try:
                await credit_user_bonus_from_bank(
                    db=db,
                    user_id=int(inviter_user_id),
                    amount=cfg.bonus,
                    reason="referral_level_bonus",
                    idempotency_key=f"REF_LVL:{int(inviter_user_id)}:{int(cfg.level)}",
                    meta={
                        "inviter_tg": int(inviter_telegram_id),
                        "level": int(cfg.level),
                        "required_active": int(cfg.required_active),
                        "active_refs": int(active_refs),
                    },
                )
            except Exception as e:
                # не роняем поток; при следующем вызове ещё раз попробуем
                logger.warning(
                    "referral level bonus failed (will retry later): inviter=%s level=%s err=%s",
                    inviter_telegram_id,
                    cfg.level,
                    e,
                )


# -----------------------------------------------------------------------------
# Подсчёт total/active рефералов и ранговая логика
# -----------------------------------------------------------------------------

async def _count_total_referrals(db: AsyncSession, inviter_telegram_id: int) -> int:
    row = await db.execute(
        text(
            f"""
            SELECT COUNT(*)
            FROM {SCHEMA}.referral_links
            WHERE inviter_telegram_id = :tg
            """
        ),
        {"tg": int(inviter_telegram_id)},
    )
    val = row.scalar() or 0
    return int(val)


async def _count_active_referrals(db: AsyncSession, inviter_telegram_id: int) -> int:
    """
    Активный реферал = есть хотя бы одна панель (исторически).
    Считаем количество уникальных пользователей-рефералов с >=1 панелью.
    """
    rows = await db.execute(
        text(
            f"""
            SELECT COUNT(DISTINCT u.id)
            FROM {SCHEMA}.referral_links rl
            JOIN {SCHEMA}.users u
              ON u.telegram_id = rl.referred_telegram_id
            JOIN {SCHEMA}.panels p
              ON p.user_id = u.id
            WHERE rl.inviter_telegram_id = :tg
            """
        ),
        {"tg": int(inviter_telegram_id)},
    )
    val = rows.scalar() or 0
    return int(val)


def _compute_level_and_progress(
    active_refs: int,
) -> Tuple[int, int, Optional[int], Optional[int], int, float]:
    """
    Возвращает:
      level, current_min, next_level, next_min, refs_to_next, progress(0..1)
    Логика:
      • Уровень 0 — до достижения первой ступени (10 активных).
      • Для уровней 1–5 используется граница по REF_LEVELS.required_active.
      • progress считает путь от текущего уровня к следующему.
    """
    if active_refs < 0:
        active_refs = 0

    if not REF_LEVELS:
        return 0, 0, None, None, 0, 0.0

    current_level = 0
    current_min = 0
    for cfg in REF_LEVELS:
        if active_refs >= cfg.required_active:
            current_level = cfg.level
            current_min = cfg.required_active
        else:
            break

    next_cfg: Optional[ReferralLevelConfig] = None
    for cfg in REF_LEVELS:
        if cfg.required_active > active_refs:
            next_cfg = cfg
            break

    if not next_cfg:
        # достигнут максимум (5 уровень)
        return current_level, current_min, None, None, 0, 1.0

    next_level = next_cfg.level
    next_min = next_cfg.required_active
    refs_to_next = max(0, next_min - active_refs)

    span = max(1, next_min - current_min)
    done = max(0, active_refs - current_min)
    progress = float(done) / float(span)
    if progress < 0.0:
        progress = 0.0
    if progress > 1.0:
        progress = 1.0

    return current_level, current_min, next_level, next_min, refs_to_next, progress


# -----------------------------------------------------------------------------
# Реферальный код и ссылка (постоянные)
# -----------------------------------------------------------------------------

async def ensure_referral_codes_table(db: AsyncSession) -> None:
    """
    Мягкое создание служебной таблицы для реф-кодов.
    Не ломает миграции (IF NOT EXISTS); может вызываться on-demand.
    """
    await db.execute(
        text(
            f"""
            CREATE TABLE IF NOT EXISTS {SCHEMA}.referral_codes (
                code TEXT PRIMARY KEY,
                inviter_telegram_id BIGINT UNIQUE NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )
    )
    await db.commit()


async def get_or_create_referral_code(
    db: AsyncSession,
    inviter_telegram_id: int,
) -> str:
    """
    Возвращает стабильный реферальный код пользователя.
    Если кода ещё нет — создаёт новый, уникальный, на основе крипто-рандома.
    Код хранится в {SCHEMA}.referral_codes.
    """
    await ensure_referral_codes_table(db)

    row = await db.execute(
        text(
            f"""
            SELECT code
            FROM {SCHEMA}.referral_codes
            WHERE inviter_telegram_id = :tg
            LIMIT 1
            """
        ),
        {"tg": int(inviter_telegram_id)},
    )
    rec = row.fetchone()
    if rec and rec[0]:
        return str(rec[0])

    for _ in range(10):
        code = _generate_referral_code()
        try:
            await db.execute(
                text(
                    f"""
                    INSERT INTO {SCHEMA}.referral_codes (code, inviter_telegram_id)
                    VALUES (:code, :tg)
                    """
                ),
                {"code": code, "tg": int(inviter_telegram_id)},
            )
            await db.commit()
            return code
        except Exception as e:
            logger.warning("referral: code collision or insert error (%s), retrying...", e)
            await db.rollback()

    fallback = _fallback_referral_code(inviter_telegram_id)
    logger.error("referral: failed to generate unique code, using fallback=%s", fallback)
    return fallback


def _generate_referral_code() -> str:
    return "".join(secrets.choice(REF_CODE_ALPHABET) for _ in range(REF_CODE_LENGTH))


def _fallback_referral_code(inviter_telegram_id: int) -> str:
    payload = json.dumps({"tg": int(inviter_telegram_id), "s": REF_SALT}, separators=(",", ":")).encode("utf-8")
    digest = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    return digest[:REF_CODE_LENGTH]


def build_referral_link(code: str) -> Optional[str]:
    """
    Собирает t.me-ссылку вида:
      https://t.me/<BOT_USERNAME>?start=ref_<code>
    Если BOT_USERNAME не задан — None.
    """
    if not BOT_USERNAME:
        return None
    code = (code or "").strip()
    if not code:
        return None
    return f"https://t.me/{BOT_USERNAME}?start=ref_{code}"


# -----------------------------------------------------------------------------
# Вспомогательные утилиты: курсор, поиск user_id
# -----------------------------------------------------------------------------

def _normalize_cursor(cursor: Optional[CursorTuple]) -> Tuple[Optional[str], Optional[int]]:
    if not cursor:
        return None, None
    try:
        ts, rid = cursor
        return (str(ts), int(rid))
    except Exception:
        return None, None


def _next_cursor(items: List[ReferralItem]) -> Optional[CursorTuple]:
    if not items:
        return None
    last = items[-1]
    return (last.invited_at.isoformat(), int(last.telegram_id))


async def _get_user_id_by_telegram(db: AsyncSession, telegram_id: int) -> Optional[int]:
    row = await db.execute(
        text(
            f"""
            SELECT id
            FROM {SCHEMA}.users
            WHERE telegram_id = :tg
            LIMIT 1
            """
        ),
        {"tg": int(telegram_id)},
    )
    rec = row.fetchone()
    if not rec:
        return None
    return int(rec[0])


# =============================================================================
# Пояснения «для чайника»:
# -----------------------------------------------------------------------------
# • Бонусы только за АКТИВНЫХ рефералов:
#     Регистрация по ссылке ничего не даёт финансово.
#     Бонусы начинаются только после первой покупки панели рефералом
#     (on_user_first_panel_purchase).
#
# • 0.1 EFHC:
#     За КАЖДОГО активного реферала пригласивший получает 0.1 EFHC на bonus-баланс,
#     но только для первых 10 000 активных рефералов. Лимит задаётся
#     REF_MAX_ACTIVE_FOR_UNIT_BONUS.
#
# • Уровни 1–5:
#     Считаются по ЧИСЛУ АКТИВНЫХ рефералов. При достижении порога уровня
#     разово начисляется бонус (1, 10, 100, 300, 1000 EFHC) на bonus-баланс
#     пригласившего. Идемпотентность: "REF_LVL:<user_id>:<level>".
#
# • Прогресс-бар:
#     get_referral_stats(...) отдаёт:
#       level (0–5), next_level, refs_to_next и progress (0.0–1.0),
#     чтобы фронтенд мог показать шкалу от текущего уровня к следующему.
#
# • Реферальная ссылка:
#     get_referral_stats(...) также даёт referral_code и referral_link.
#     Формат ссылки: https://t.me/<BOT_USERNAME>?start=ref_<code>.
#
# • Идемпотентность:
#     Все денежные операции используют жёсткие Idempotency-Key:
#       - "REF_ACT_UNIT:<buyer_tg>"         — 0.1 EFHC за активного реферала.
#       - "REF_LVL:<user_id>:<level>"      — бонус за достижение уровня.
#   Повторные вызовы безопасны — банк не создаст дубль записи.
# =============================================================================

__all__ = [
    # Списки для фронтенда
    "ReferralItem",
    "CursorTuple",
    "svc_list_referrals_active",
    "svc_list_referrals_inactive",
    # Триггеры
    "register_referral_and_reward",
    "on_user_first_panel_purchase",
    # Статистика / ссылка
    "ReferralStats",
    "get_referral_stats",
    "get_or_create_referral_code",
    "build_referral_link",
]
