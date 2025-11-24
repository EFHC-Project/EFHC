# -*- coding: utf-8 -*-
# backend/app/models/rating_models.py
# =============================================================================
# Назначение кода:
#   ORM-модели домена «Рейтинг» EFHC Bot:
#   • RatingSnapshot  — исторические снапшоты позиций пользователей по total_generated_kwh.
#   • RatingTopCache  — материализованный TOP-N для быстрых витрин «Я + TOP».
#
# Канон/инварианты:
#   • Источник ранжирования — только users.total_generated_kwh (пер-сек генерация; суточных ставок нет).
#   • Снапшот фиксируется по моменту snapshot_at; для (snapshot_at, user_id) не допускаются дубликаты.
#   • Денежная/энергетическая точность — Numeric(30,8); округление вниз выполняется в сервисах.
#   • «Я + TOP»: в выдаче нельзя дублировать «Я» — логика на уровне сервиса/роутов.
#
# ИИ-защиты:
#   • Индексы под курсорную пагинацию без OFFSET (created_at,id) и быстрый выбор TOP по snapshot_at.
#   • Уникальные ограничения защищают от гонок при параллельной сборке снапшотов/кэшей.
#   • meta(JSONB) — безопасное расширение (например, «scope»: "global" | "weekly", «note»: "...").
#
# Запреты:
#   • Модели не выполняют пересчётов — только хранят результат. Пересчёты делает ranks_service/scheduler.
#   • Никаких «суточных» полей; рейтинг строится из total_generated_kwh (пер-сек аккумулируемое).
# =============================================================================

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Numeric

from ..core.config_core import get_settings
from ..core.database_core import Base  # единый declarative Base проекта

settings = get_settings()
CORE_SCHEMA = settings.DB_SCHEMA_CORE  # например, "efhc_core"


# -----------------------------------------------------------------------------
# Исторический снапшот рейтинга по пользователю
# -----------------------------------------------------------------------------
class RatingSnapshot(Base):
    """
    Историческая позиция пользователя в рейтинге на момент snapshot_at.

    Поля:
      • snapshot_at           — момент формирования снапшота (UTC, с TZ).
      • user_id               — Telegram ID пользователя.
      • rank_position         — позиция в рейтинге (1 — лидер).
      • total_generated_kwh   — тотал энергии на момент снапшота (Decimal(30,8)).
      • meta                  — расширения (например, {"scope":"global"}).

    Инварианты:
      • (snapshot_at, user_id) — уникальны (не допускаем дублей одного времени).
      • total_generated_kwh >= 0, rank_position >= 1.
    """

    __tablename__ = "rating_snapshots"
    __table_args__ = (
        UniqueConstraint("snapshot_at", "user_id", name="uq_rating_snapshot_user_at"),
        CheckConstraint("total_generated_kwh >= 0", name="ck_rating_snapshot_total_nonneg"),
        CheckConstraint("rank_position >= 1", name="ck_rating_snapshot_rank_pos"),
        {"schema": CORE_SCHEMA},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    snapshot_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)

    rank_position: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    total_generated_kwh: Mapped[str] = mapped_column(Numeric(30, 8), nullable=False)

    meta: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB, nullable=True)

    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return f"<RatingSnapshot uid={self.user_id} pos={self.rank_position} at={self.snapshot_at.isoformat()}>"


# Быстрые индексы под курсоры и выбор TOP-N на конкретный момент
Index("ix_rating_snapshots_created_id", RatingSnapshot.created_at, RatingSnapshot.id,
      postgresql_using="btree", schema=CORE_SCHEMA)
Index("ix_rating_snapshots_at_pos", RatingSnapshot.snapshot_at, RatingSnapshot.rank_position,
      postgresql_using="btree", schema=CORE_SCHEMA)
Index("ix_rating_snapshots_user_at", RatingSnapshot.user_id, RatingSnapshot.snapshot_at,
      postgresql_using="btree", schema=CORE_SCHEMA)


# -----------------------------------------------------------------------------
# Материализованный TOP-N (витрина «Я + TOP»)
# -----------------------------------------------------------------------------
class RatingTopCache(Base):
    """
    Кэш-листинг TOP-N на момент snapshot_at для мгновенной отдачи фронту.

    Поля:
      • snapshot_at           — момент формирования топа (тот же, что и в снапшотах).
      • user_id               — участник топа.
      • rank_position         — позиция в топе (1..N).
      • total_generated_kwh   — тотал энергии (Decimal(30,8)).
      • bucket                — вариант витрины (например: "global_top_100"); для сегментации кэшей.
      • meta                  — расширения (например, {"note":"precomputed for /rating"}).

    Инварианты:
      • (bucket, snapshot_at, user_id) — уникально (не дублируем записи для одного пользователя).
      • Также уникален (bucket, snapshot_at, rank_position) — позиция не может принадлежать двум.
      • total_generated_kwh >= 0, rank_position >= 1.
    """

    __tablename__ = "rating_top_cache"
    __table_args__ = (
        UniqueConstraint("bucket", "snapshot_at", "user_id", name="uq_rating_top_user_at_bucket"),
        UniqueConstraint("bucket", "snapshot_at", "rank_position", name="uq_rating_top_pos_at_bucket"),
        CheckConstraint("total_generated_kwh >= 0", name="ck_rating_top_total_nonneg"),
        CheckConstraint("rank_position >= 1", name="ck_rating_top_rank_pos"),
        {"schema": CORE_SCHEMA},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    bucket: Mapped[str] = mapped_column(String(32), nullable=False, index=True, default="global_top_100", server_default="global_top_100")
    snapshot_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, index=True)

    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    rank_position: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    total_generated_kwh: Mapped[str] = mapped_column(Numeric(30, 8), nullable=False)

    meta: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB, nullable=True)

    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return f"<RatingTopCache bucket={self.bucket} pos={self.rank_position} uid={self.user_id} at={self.snapshot_at.isoformat()}>"


# Индексы под курсор/витрины
Index("ix_rating_top_cache_created_id", RatingTopCache.created_at, RatingTopCache.id,
      postgresql_using="btree", schema=CORE_SCHEMA)
Index("ix_rating_top_cache_bucket_at_pos", RatingTopCache.bucket, RatingTopCache.snapshot_at, RatingTopCache.rank_position,
      postgresql_using="btree", schema=CORE_SCHEMA)


__all__ = [
    "RatingSnapshot",
    "RatingTopCache",
]
# =============================================================================
# Пояснения «для чайника»:
#   • Зачем два класса?
#     RatingSnapshot — подробная история рангов для аналитики/ретроспективы.
#     RatingTopCache — маленькая материализованная выборка TOP-N для мгновенной отдачи фронту,
#     чтобы /rating работал стабильно и быстро.
#
#   • Откуда берутся данные?
#     ranks_service собирает срез из users.total_generated_kwh, сортирует по убыванию,
#     присваивает rank_position и записывает в обе таблицы. Планировщик update_rating
#     делает это регулярно и «догоняет» при сбоях.
#
#   • Как избежать дубликатов «Я» в выдаче?
#     Это правило реализуется в сервисе/роутере при формировании «Я + TOP»:
#     если «Я» есть в TOP-N, отдельно не добавляем, если нет — подмешиваем позицию «Я»
#     с реальным местом и тоталом, не нарушая основной TOP-N.
# =============================================================================
