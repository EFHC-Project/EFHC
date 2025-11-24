# -*- coding: utf-8 -*-
# backend/app/models/referrals_models.py
# =============================================================================
# Назначение кода:
#   ORM-модели домена «Рефералы» EFHC Bot:
#   • ReferralLink — связь «пригласивший → приглашённый», с фиксацией момента активации.
#
# Канон/инварианты:
#   • «Активный реферал» — это приглашённый, который купил хотя бы одну панель.
#     В модели это отражается полем activated_at (NULL → неактивный; NOT NULL → активный).
#   • У одного пользователя может быть только один «родитель» (первичный пригласивший).
#   • Денежные вознаграждения за рефералов НЕ хранятся здесь — только в банковском журнале
#     (efhc_transfers_log) через сервисы. Эта модель — про структуру связей и статусы.
#
# ИИ-защиты:
#   • Индексы (created_at, id) под курсорную пагинацию витрин «Активные/Неактивные».
#   • Жёсткие ограничения на данные (нельзя пригласить самого себя; уникальный «родитель»).
#   • Поле meta (JSONB) для расширения без миграций (кампания, источник, метки).
#
# Запреты:
#   • Никаких денежных полей/начислений — деньги двигает только банковский сервис.
#   • Никаких «уровней» в модели — уровни/витрины считает referral_service.
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
from sqlalchemy.types import Numeric  # оставлено для консистентности импорта в моделях
from ..core.config_core import get_settings
from ..core.database_core import Base  # единый declarative Base проекта

settings = get_settings()
CORE_SCHEMA = settings.DB_SCHEMA_CORE  # например, "efhc_core"


# -----------------------------------------------------------------------------
# Связь «пригласивший → приглашённый»
# -----------------------------------------------------------------------------
class ReferralLink(Base):
    """
    Единая запись реферальной связи. Создаётся при регистрации приглашённого по реф-коду.

    Поля:
      • referrer_id  — Telegram ID пригласившего.
      • referee_id   — Telegram ID приглашённого.
      • created_at   — когда связь была создана (регистрация по реф-ссылке).
      • activated_at — когда приглашённый стал активным (первая покупка панели).
      • campaign     — код рекламной кампании/источника (опционально).
      • meta         — произвольные пометки (UTM, платформы, подидентификаторы).

    Инварианты:
      • Один приглашённый может иметь только одного пригласившего (UNIQUE referee_id).
      • Нельзя пригласить самого себя (CHECK referrer_id != referee_id).
      • «Активность» — это просто факт наличия activated_at (NOT NULL).
    """

    __tablename__ = "referral_links"
    __table_args__ = (
        UniqueConstraint("referee_id", name="uq_referral_links_referee"),
        CheckConstraint("referrer_id <> referee_id", name="ck_referral_not_self"),
        {"schema": CORE_SCHEMA},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Кто пригласил (Telegram ID)
    referrer_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)

    # Кого пригласили (Telegram ID)
    referee_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)

    # Метки времени: создание связи и момент «активации» (первая покупка панели)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False)
    activated_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True), nullable=True, index=True)

    # Опционально: источник/кампания и произвольные данные
    campaign: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    meta: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB, nullable=True)

    def __repr__(self) -> str:
        active = self.activated_at is not None
        return f"<ReferralLink id={self.id} {self.referrer_id}->{self.referee_id} active={active}>"


# Индексы под курсор/витрины и фильтры «Активные/Неактивные»
Index("ix_ref_links_created_id", ReferralLink.created_at, ReferralLink.id, postgresql_using="btree", schema=CORE_SCHEMA)
Index("ix_ref_links_referrer_created", ReferralLink.referrer_id, ReferralLink.created_at,
      postgresql_using="btree", schema=CORE_SCHEMA)
Index("ix_ref_links_activated_at", ReferralLink.activated_at, postgresql_using="btree", schema=CORE_SCHEMA)


# -----------------------------------------------------------------------------
# Экспорт
# -----------------------------------------------------------------------------
__all__ = [
    "ReferralLink",
]
# =============================================================================
# Пояснения «для чайника»:
#   • Почему у одного приглашённого может быть только один «родитель»?
#     Это предотвращает «перехваты» и двусмысленность начислений. Первичный реферер фиксируется
#     при регистрации, а денежные бонусы (если предусмотрены) рассчитываются в сервисе.
#
#   • Откуда берётся «активность»?
#     Сервис рефералов/панелей при первой покупке панели приглашённым проставляет activated_at,
#     после чего такой реферал считается активным во всех витринах/расчётах.
#
#   • Где деньги за рефералов?
#     Денежные операции (в т.ч. бонусы) пишутся только в банковский журнал (efhc_transfers_log)
#     через единый банковский сервис. Здесь — только структура связей и статусы.
# =============================================================================
