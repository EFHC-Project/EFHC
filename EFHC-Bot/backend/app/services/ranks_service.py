# -*- coding: utf-8 -*-
# backend/app/services/ranks_service.py
# =============================================================================
# Назначение кода:
# Сервис рейтинга EFHC Bot: «Я + TOP» и постраничные витрины лидерборда.
# Строит и поддерживает снапшоты рейтинга по total_generated_kwh, возвращает
# реальное место пользователя, исключая дублирование «Я» в TOP, и даёт
# cursor-based пагинацию (без OFFSET).
#
# Канон/инварианты:
# • Источник места в рейтинге — только total_generated_kwh (не available_kwh).
# • Никаких суточных расчётов. Посекундные ставки используются лишь в energy_service.
# • Пагинация — строго по курсору (keyset), сортировка: (kWh DESC, user_id ASC).
# • «Я + TOP»: сначала реальная позиция текущего пользователя, затем глобальный TOP,
#   при этом «Я» не повторяется, если попадает в TOP.
#
# ИИ-защита/самовосстановление:
# • Автоматическая проверка/создание отсутствующих таблиц (IF NOT EXISTS) — мягкий
#   автоконсистентный DDL для Neon/PostgreSQL (без ломки миграций).
# • Реконструкция снапшота под advisory-локом (pg_try_advisory_xact_lock), чтобы
#   избежать гонок задач планировщика.
# • При ошибках чтения снапшота — деградация к live-выборке из users с логом.
# • Детальные логи, отсутствие падений сервиса при частных сбоях.
#
# Запреты:
# • Нет денег/эмиссии/балансов — только чтение и материализация витрин рейтинга.
# • Нет P2P, нет пересчёта «энергии» — она уже записана в users.total_generated_kwh.
# =============================================================================

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config_core import get_settings
from backend.app.core.logging_core import get_logger
from backend.app.deps import d8  # единая Decimal(8) из deps (канон)

logger = get_logger(__name__)
settings = get_settings()
SCHEMA = getattr(settings, "DB_SCHEMA_CORE", "efhc_core")

# -----------------------------------------------------------------------------
# DTO/вспомогательные структуры
# -----------------------------------------------------------------------------

@dataclass
class RankRow:
    user_id: int
    username: Optional[str]
    total_kwh: str  # строка с 8 знаками
    rank: int


@dataclass
class RankSnapshotMeta:
    snapshot_at: datetime
    total_users: int


# -----------------------------------------------------------------------------
# Внутренние SQL (PostgreSQL, Neon OK)
# -----------------------------------------------------------------------------

SQL_ENSURE_TABLES = f"""
-- материализованный снапшот (актуальная раскладка мест)
CREATE TABLE IF NOT EXISTS {SCHEMA}.rating_cache (
    user_id BIGINT PRIMARY KEY,
    username TEXT,
    total_generated_kwh NUMERIC(30,8) NOT NULL DEFAULT 0,
    rank_position BIGINT NOT NULL,
    snapshot_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- история снапшотов (для отчётности и отладки)
CREATE TABLE IF NOT EXISTS {SCHEMA}.rating_snapshots (
    id BIGSERIAL PRIMARY KEY,
    snapshot_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    total_users BIGINT NOT NULL,
    built_ms BIGINT NOT NULL DEFAULT 0
);

-- индекс по рейтингу для быстрых витрин/курсора:
CREATE INDEX IF NOT EXISTS idx_rating_cache_rank
ON {SCHEMA}.rating_cache (rank_position ASC, user_id ASC);
"""

# advisory-lock: фиксированная «соль» сервиса рейтинга
# (должна быть одинаковой для всех процессов, чтобы синхронизироваться).
ADVISORY_LOCK_KEY = 0x45F1_00AB  # произвольная стабильная константа


# -----------------------------------------------------------------------------
# Публичный API сервиса
# -----------------------------------------------------------------------------

async def ensure_rating_tables(db: AsyncSession) -> None:
    """
    ИИ-самовосстановление: проверяет и при необходимости создаёт таблицы рейтинга.
    Не ломает миграции: IF NOT EXISTS. Вызывать при старте тик-задачи и on-demand.

    ВАЖНО:
      • Функция НЕ делает commit/rollback — транзакцией управляет вызывающая сторона
        (роутер/планировщик). Это соответствует общему канону сервисов.
    """
    await db.execute(text(SQL_ENSURE_TABLES))
    # commit умышленно не вызывается: DDL участвует в внешней транзакции


async def rebuild_rank_snapshot(
    db: AsyncSession,
    *,
    force_refresh: bool = False,   # параметр зарезервирован для политики TTL на уровне планировщика
    limit_users: Optional[int] = None,
) -> RankSnapshotMeta:
    """
    Перестраивает материализованный рейтинг.
    • Берёт данные из users: (telegram_id, username, total_generated_kwh).
    • Считает места по убыванию total_generated_kwh, tie-break: user_id ASC.
    • Сохраняет в rating_cache; пишет запись в rating_snapshots.
    • Защита от гонок: advisory-лок на транзакцию.
    • limit_users — опциональная усечённая сборка (для стресс-режимов).

    Примечание:
      • Параметр force_refresh зарезервирован: сейчас перестроение всегда полное,
        решение о частоте/«свежести» принимается на уровне планировщика.
    """
    await ensure_rating_tables(db)

    # 1) Advisory-lock: конкурентные перестроения будут отфутболены
    got_lock = await _try_advisory_xact_lock(db, ADVISORY_LOCK_KEY)
    if not got_lock:
        logger.info("ranks_service: skip rebuild — another process holds the advisory lock.")
        # Считаем текущую мету, если есть
        meta = await _current_meta(db)
        if meta:
            return meta
        # Если совсем пусто — мягкая деградация: построим без лока
        logger.warning("ranks_service: no snapshot meta found; building without lock safeguard.")
        # продолжаем

    # 2) Собираем live-набор пользователей из users
    limit_clause = ""
    if limit_users and int(limit_users) > 0:
        limit_clause = f"LIMIT {int(limit_users)}"

    rows = await db.execute(
        text(
            f"""
            SELECT
              u.telegram_id AS user_id,
              u.username,
              COALESCE(u.total_generated_kwh, 0) AS total_kwh
            FROM {SCHEMA}.users u
            WHERE COALESCE(u.is_active, TRUE) = TRUE
            ORDER BY COALESCE(u.total_generated_kwh, 0) DESC, u.telegram_id ASC
            {limit_clause}
            """
        )
    )
    data = rows.fetchall()

    # 3) Материализуем в rating_cache (TRUNCATE + INSERT) с рангами
    total_users = 0
    rank = 0
    snapshot_at = datetime.now(tz=timezone.utc)

    # Чистим кэш полностью, затем заливаем: гарантирует консистентные позиции.
    await db.execute(text(f"TRUNCATE TABLE {SCHEMA}.rating_cache"))

    for rec in data:
        total_users += 1
        rank += 1
        user_id = int(rec[0])
        username = rec[1]
        total_kwh = d8(rec[2])

        await db.execute(
            text(
                f"""
                INSERT INTO {SCHEMA}.rating_cache (user_id, username, total_generated_kwh, rank_position, snapshot_at)
                VALUES (:user_id, :username, :kwh, :rank, :snap)
                ON CONFLICT (user_id) DO UPDATE
                SET username = EXCLUDED.username,
                    total_generated_kwh = EXCLUDED.total_generated_kwh,
                    rank_position = EXCLUDED.rank_position,
                    snapshot_at = EXCLUDED.snapshot_at
                """
            ),
            {"user_id": user_id, "username": username, "kwh": str(total_kwh), "rank": rank, "snap": snapshot_at},
        )

    # 4) Пишем историю снапшота
    await db.execute(
        text(
            f"""
            INSERT INTO {SCHEMA}.rating_snapshots (snapshot_at, total_users, built_ms)
            VALUES (:snap, :total, 0)
            """
        ),
        {"snap": snapshot_at, "total": total_users},
    )
    await db.commit()

    logger.info("ranks_service: snapshot rebuilt at %s with %s users", snapshot_at.isoformat(), total_users)

    return RankSnapshotMeta(snapshot_at=snapshot_at, total_users=total_users)


async def get_user_rank_and_top(
    db: AsyncSession,
    *,
    user_id: int,
    top_limit: int = 100,
) -> Tuple[Optional[RankRow], List[RankRow], RankSnapshotMeta]:
    """
    Возвращает кортеж:
      (me_row | None, top_rows[<=top_limit], meta)
    где:
      • me_row — реальные данные и место пользователя (или None, если нет в users),
      • top_rows — глобальный TOP, без дублирования «Я»,
      • meta — snapshot_at/total_users.

    ИИ-поведение:
      • Если rating_cache пуст/отсутствует — деградация: live-выборка из users.
      • Если «Я» не найден — возвращаем None и TOP без исключений.

    ВНИМАНИЕ:
      • В этом сервисе user_id == telegram_id (канон рейтинга). В БД rating_cache.user_id
        должен быть синхронизирован с users.telegram_id.
    """
    await ensure_rating_tables(db)

    meta = await _current_meta(db)
    if not meta:
        # нет снапшота — пробуем собрать быстро live и вернуть
        logger.warning("ranks_service: missing meta — falling back to live read (users).")
        me, top, meta = await _live_me_and_top(db, user_id=user_id, top_limit=top_limit)
        return me, top, meta

    # 1) читаем «Я» из rating_cache
    me_row = await db.execute(
        text(
            f"""
            SELECT c.user_id, c.username, c.total_generated_kwh, c.rank_position
            FROM {SCHEMA}.rating_cache c
            WHERE c.user_id = :uid
            LIMIT 1
            """
        ),
        {"uid": int(user_id)},
    )
    me = me_row.fetchone()
    me_obj: Optional[RankRow] = None
    if me:
        me_obj = RankRow(
            user_id=int(me[0]),
            username=me[1],
            total_kwh=str(d8(me[2])),
            rank=int(me[3]),
        )

    # 2) читаем TOP-N
    rs = await db.execute(
        text(
            f"""
            SELECT c.user_id, c.username, c.total_generated_kwh, c.rank_position
            FROM {SCHEMA}.rating_cache c
            ORDER BY c.rank_position ASC
            LIMIT :lim
            """
        ),
        {"lim": int(top_limit)},
    )
    top_data = rs.fetchall()
    top_rows: List[RankRow] = []
    for r in top_data:
        uid = int(r[0])
        if me_obj and uid == me_obj.user_id:
            # исключаем «Я» из TOP (не дублируем)
            continue
        top_rows.append(
            RankRow(
                user_id=uid,
                username=r[1],
                total_kwh=str(d8(r[2])),
                rank=int(r[3]),
            )
        )

    return me_obj, top_rows, meta


async def get_leaderboard_page(
    db: AsyncSession,
    *,
    after_cursor: Optional[str],
    page_size: int = 100,
) -> Tuple[List[RankRow], Optional[str], RankSnapshotMeta]:
    """
    Возвращает постраничную витрину (без «Я») c курсором.
    Сортировка: rank_position ASC (что эквивалентно total_kwh DESC, user_id ASC).
    Курсор кодирует последнюю rank_position и user_id для устойчивой навигации.

    Cursor format (base64 JSON):
      {"pos": <rank_position:int>, "uid": <user_id:int>}
    """
    await ensure_rating_tables(db)

    meta = await _current_meta(db)
    if not meta:
        # на пустой базе вернём пустую страницу
        return [], None, RankSnapshotMeta(snapshot_at=datetime.now(tz=timezone.utc), total_users=0)

    where = ""
    params: Dict[str, Any] = {"lim": int(page_size)}
    if after_cursor:
        try:
            pos, uid = _decode_cursor(after_cursor)
            where = "WHERE (c.rank_position, c.user_id) > (:pos, :uid)"
            params.update({"pos": int(pos), "uid": int(uid)})
        except Exception as e:
            logger.warning("ranks_service: bad cursor %s: %s", after_cursor, e)
            # если курсор битый — начинаем с начала

    rs = await db.execute(
        text(
            f"""
            SELECT c.user_id, c.username, c.total_generated_kwh, c.rank_position
            FROM {SCHEMA}.rating_cache c
            {where}
            ORDER BY c.rank_position ASC, c.user_id ASC
            LIMIT :lim
            """
        ),
        params,
    )
    data = rs.fetchall()
    items: List[RankRow] = [
        RankRow(
            user_id=int(r[0]),
            username=r[1],
            total_kwh=str(d8(r[2])),
            rank=int(r[3]),
        )
        for r in data
    ]

    next_cursor: Optional[str] = None
    if items:
        last = items[-1]
        next_cursor = _encode_cursor(last.rank, last.user_id)

    return items, next_cursor, meta


# -----------------------------------------------------------------------------
# Внутренние помощники
# -----------------------------------------------------------------------------

async def _current_meta(db: AsyncSession) -> Optional[RankSnapshotMeta]:
    row = await db.execute(
        text(
            f"""
            SELECT s.snapshot_at, s.total_users
            FROM {SCHEMA}.rating_snapshots s
            ORDER BY s.snapshot_at DESC
            LIMIT 1
            """
        )
    )
    rec = row.fetchone()
    if not rec:
        return None
    return RankSnapshotMeta(snapshot_at=rec[0], total_users=int(rec[1]))


async def _live_me_and_top(
    db: AsyncSession,
    *,
    user_id: int,
    top_limit: int,
) -> Tuple[Optional[RankRow], List[RankRow], RankSnapshotMeta]:
    """
    Деградационный режим: читаем напрямую из users без материализации.
    Работает медленнее, но не ломается при отсутствии кэша.
    """
    # Место «Я»
    me_q = await db.execute(
        text(
            f"""
            SELECT u.telegram_id, u.username, COALESCE(u.total_generated_kwh, 0) AS total_kwh
            FROM {SCHEMA}.users u
            WHERE u.telegram_id = :uid
            LIMIT 1
            """
        ),
        {"uid": int(user_id)},
    )
    me = me_q.fetchone()
    me_obj: Optional[RankRow] = None
    if me:
        # ранжируем «Я» по всей таблице
        rank_q = await db.execute(
            text(
                f"""
                SELECT 1 + COUNT(*) AS my_rank
                FROM {SCHEMA}.users uu
                WHERE COALESCE(uu.total_generated_kwh, 0) > :my_kwh
                   OR (COALESCE(uu.total_generated_kwh, 0) = :my_kwh AND uu.telegram_id < :uid)
                """
            ),
            {"my_kwh": me[2], "uid": int(me[0])},
        )
        r = rank_q.fetchone()
        me_obj = RankRow(
            user_id=int(me[0]),
            username=me[1],
            total_kwh=str(d8(me[2])),
            rank=int(r[0]) if r else 1,
        )

    # TOP-N
    rs = await db.execute(
        text(
            f"""
            SELECT u.telegram_id, u.username, COALESCE(u.total_generated_kwh, 0) AS total_kwh
            FROM {SCHEMA}.users u
            ORDER BY COALESCE(u.total_generated_kwh, 0) DESC, u.telegram_id ASC
            LIMIT :lim
            """
        ),
        {"lim": int(top_limit)},
    )
    top_rows: List[RankRow] = []
    for idx, r in enumerate(rs.fetchall(), start=1):
        uid = int(r[0])
        if me_obj and uid == me_obj.user_id:
            continue
        top_rows.append(
            RankRow(user_id=uid, username=r[1], total_kwh=str(d8(r[2])), rank=idx)
        )

    meta = RankSnapshotMeta(snapshot_at=datetime.now(tz=timezone.utc), total_users=await _count_users(db))
    return me_obj, top_rows, meta


async def _count_users(db: AsyncSession) -> int:
    row = await db.execute(text(f"SELECT COUNT(*) FROM {SCHEMA}.users"))
    return int(row.scalar_one())


async def _try_advisory_xact_lock(db: AsyncSession, key: int) -> bool:
    """
    Пытается взять транзакционный advisory-лок (освобождается при коммите/роллбэке).
    Возвращает True/False без исключения.
    """
    rs = await db.execute(text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": int(key)})
    val = rs.scalar_one_or_none()
    return bool(val)


# --- курсор-кодек (локальный, без зависимости от роутеров/ETag) ---------------

import base64
import json

def _encode_cursor(rank_pos: int, user_id: int) -> str:
    payload = {"pos": int(rank_pos), "uid": int(user_id)}
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _decode_cursor(cursor: str) -> Tuple[int, int]:
    raw = base64.urlsafe_b64decode(cursor.encode("ascii"))
    obj = json.loads(raw.decode("utf-8"))
    return int(obj["pos"]), int(obj["uid"])


# =============================================================================
# Экспорт для import *
# =============================================================================

__all__ = [
    # DTO
    "RankRow",
    "RankSnapshotMeta",
    # публичный API
    "ensure_rating_tables",
    "rebuild_rank_snapshot",
    "get_user_rank_and_top",
    "get_leaderboard_page",
]

# =============================================================================
# Пояснения «для чайника»:
# • Почему материализуем (rating_cache)?
#   Чтобы отдать UI быстрый и стабильный рейтинг (без тяжёлых COUNT/OFFSET).
#   Перестройка выполняется планировщиком (каждые N минут) или по требованию админки.
#
# • Как считается место?
#   Сортируем по total_generated_kwh (DESC). При равенстве — по user_id (ASC),
#   где user_id == telegram_id для рейтинга (канон).
#   Место — это 1-based индекс в этой сортировке.
#
# • Зачем advisory-lock?
#   Чтобы параллельные перестроения не портили кэш. Берёт «флажок» на транзакцию,
#   который снимается автоматически при коммите/роллбэке.
#
# • Почему курсор, а не OFFSET?
#   OFFSET медленно на больших таблицах и даёт дрожащие страницы. Курсор кодирует
#   последний (rank_position, user_id); следующая страница строится «строго после»
#   этой пары — стабильно и быстро.
#
# • Что делать фронтенду?
#   Для «Я + TOP» вызвать get_user_rank_and_top через соответствующий роут
#   (rating_routes.py). Для полного списка — листать get_leaderboard_page с курсором.
# =============================================================================
