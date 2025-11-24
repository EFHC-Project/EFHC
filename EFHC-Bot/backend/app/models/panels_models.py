# -*- coding: utf-8 -*-
# backend/app/models/panels_models.py
# =============================================================================
# Назначение кода:
#   ORM-модель домена «Панели» EFHC Bot:
#   • Panel — активные и архивные панели пользователя в одной таблице с флагом is_active.
#
# Канон/инварианты:
#   • Генерация энергии считается только в сервисах (per-sec ставки GEN_PER_SEC_BASE_KWH / GEN_PER_SEC_VIP_KWH).
#   • Никаких «суточных» полей. Все суммы энергии — Decimal(30,8), без отрицательных значений.
#   • Архивация панели: is_active=false, archived_at NOT NULL; покупка новой — через банк (в сервисах).
#   • Лимит на пользователя: 1000 активных панелей (контроль — в сервисах).
#
# ИИ-защиты:
#   • Индексы под курсорную пагинацию (created_at,id) и выборки по пользователю/активности/сроку.
#   • Поля last_generated_at и generated_kwh — для «догон-начисления» при сбоях/перезапусках.
#   • meta(JSONB) — безопасное расширение (например, источник покупки, SKU, примечания аудита).
#
# Запреты:
#   • Модель НЕ выполняет расчётов/списаний/начислений и не проверяет лимиты — это делает сервис.
#   • Никакой автокоррекции балансов пользователя — только чтение/хранение фактов о панелях.
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
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Numeric

from ..core.config_core import get_settings
from ..core.database_core import Base  # единый declarative Base проекта

settings = get_settings()
CORE_SCHEMA = settings.DB_SCHEMA_CORE  # например, "efhc_core"


class Panel(Base):
    """
    Панель пользователя (активная или архивная).

    Поля:
      • user_id            — Telegram ID владельца.
      • is_active          — флаг активности; FALSE ⇒ панель в архиве.
      • created_at         — дата создания (момент покупки).
      • expires_at         — дата окончания срока (например, +180 дней от покупки).
      • archived_at        — дата архивирования (для неактивных панелей).
      • last_generated_at  — последний момент, до которого начислена энергия «догоном».
      • base_gen_per_sec   — ставка генерации, зафиксированная для панели при создании (Decimal(30,8));
                              хранится для аудита/витрин, но фактический расчёт ведётся по текущему is_vip пользователя.
      • generated_kwh      — накопленная энергия по панели (Decimal(30,8), ≥ 0).
      • title              — человеко-читаемое имя/серия (опционально для UI).
      • meta               — произвольные тех.данные (SKU, источник заказа и т.д.).
    """

    __tablename__ = "panels"
    __table_args__ = (
        # Никаких отрицательных энергий/ставок
        CheckConstraint("generated_kwh >= 0", name="ck_panels_generated_nonneg"),
        CheckConstraint("base_gen_per_sec >= 0", name="ck_panels_base_rate_nonneg"),
        {"schema": CORE_SCHEMA},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Идентификация владельца
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)

    # Статус жизненного цикла
    is_active: Mapped[bool] = mapped_column(nullable=False, default=True, server_default="true", index=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False)
    expires_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True), nullable=True, index=True)
    archived_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True), nullable=True, index=True)

    # Данные для «догон-начисления»
    last_generated_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True), nullable=True, index=True)

    # Ставка (для аудита) и накопленная энергия
    base_gen_per_sec: Mapped[str] = mapped_column(Numeric(30, 8), nullable=False, default="0", server_default="0")
    generated_kwh: Mapped[str] = mapped_column(Numeric(30, 8), nullable=False, default="0", server_default="0")

    # UI/расширения
    title: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    meta: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB, nullable=True)

    # Авто-обновление updated_at
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        st = "active" if self.is_active else "archived"
        return f"<Panel id={self.id} user={self.user_id} {st} gen={self.generated_kwh}>"


# Индексы под курсоры и быстрые выборки
Index("ix_panels_created_id", Panel.created_at, Panel.id, postgresql_using="btree", schema=CORE_SCHEMA)
Index("ix_panels_user_active", Panel.user_id, Panel.is_active, postgresql_using="btree", schema=CORE_SCHEMA)
Index("ix_panels_user_expire", Panel.user_id, Panel.expires_at, postgresql_using="btree", schema=CORE_SCHEMA)

__all__ = [
    "Panel",
]
# =============================================================================
# Пояснения «для чайника»:
#   • Где хранится «архив»?
#     В той же таблице: is_active=false и заполнено archived_at. Это упрощает витрины и миграции.
#
#   • Зачем base_gen_per_sec в панели, если расчёт по пользователю?
#     Это аудиторское поле и подсказка для UI. Фактическое начисление делает energy_service,
#     опираясь на текущий is_vip пользователя и канонические GEN_PER_SEC_*.
#
#   • Что делает last_generated_at?
#     Помогает планировщику «догонять» начисления: сколько времени с последнего тика надо
#     домножить на ставку при восстановлении после сбоя/перезапуска.
# =============================================================================
