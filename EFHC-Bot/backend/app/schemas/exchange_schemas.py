# -*- coding: utf-8 -*-
# backend/app/schemas/exchange_schemas.py
# =============================================================================
# Назначение кода:
# Pydantic-схемы для раздела «Обмен энергии (kWh) → EFHC».
# Описывают вход/выход API: предпросмотр, конвертацию, историю обменов.
#
# Канон/инварианты:
# • Обмен возможен ТОЛЬКО в одну сторону: kWh → EFHC по фикс. курсу 1:1.
# • Все суммы — Decimal c 8 знаками, округление вниз (канон EFHC_DECIMALS=8).
# • Денежные POST обязаны иметь Idempotency-Key (в заголовке; в теле — опц. client_nonce).
# • Пользователь НЕ может уходить в минус; банк МОЖЕТ (не блокируем операции).
#
# ИИ-защита:
# • Валидация и квантизация Decimal до 8 знаков на входе/выходе.
# • Защита от отрицательных и нулевых обменов (строго > 0).
# • Поле client_nonce — дополнительный «предохранитель» идемпотентности на стороне клиента.
#
# Запреты:
# • НЕТ обратному обмену EFHC → kWh.
# • НЕТ P2P-переводам — только «пользователь ↔ банк» (в сервисах).
# =============================================================================

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import List, Optional

from pydantic import BaseModel, Field, condecimal, constr, validator

# Общее: курсорная пагинация, стандартные ответы/ошибки (реэкспортируемые схемы)
from backend.app.schemas.common_schemas import CursorPage
# Централизованная квантизация Decimal(8)
from backend.app.deps import d8

# -----------------------------------------------------------------------------
# Константы домена (только для документации и дефолтов схем; бизнес-логика — в сервисах)
# -----------------------------------------------------------------------------
KWH_TO_EFHC_RATE: Decimal = Decimal("1.0")  # канон: строго 1:1

# =============================================================================
# Предпросмотр: «сколько доступно обменять прямо сейчас»
# =============================================================================


class ExchangePreviewOut(BaseModel):
    """
    Выход предпросмотра обмена (ничего не списывает).
    Поля служат только для UI-подсказки.
    """

    ok: bool = Field(..., description="Готовность к обмену (true — можно обменять).")
    available_kwh: Decimal = Field(
        ..., description="Текущая доступная к обмену энергия, kWh (Decimal/8)."
    )
    max_exchangeable_kwh: Decimal = Field(
        ..., description="Максимум, который можно обменять сейчас, kWh."
    )
    rate_kwh_to_efhc: Decimal = Field(
        KWH_TO_EFHC_RATE, description="Фиксированный курс 1.0 (kWh→EFHC)."
    )
    detail: str = Field(
        "", description="Текстовая подсказка/причина (например, «нет доступной энергии»)."
    )

    # Квантизация/нормализация всех Decimal полей
    @validator("available_kwh", "max_exchangeable_kwh", "rate_kwh_to_efhc", pre=True, always=True)
    def _q8(cls, v: Decimal) -> Decimal:
        return d8(v)

    class Config:
        json_encoders = {Decimal: lambda v: str(d8(v))}


# =============================================================================
# Конвертация kWh → EFHC
# =============================================================================


class ExchangeConvertIn(BaseModel):
    """
    Вход POST обмена: сколько kWh обменять.
    Идемпотентность обеспечивается заголовком Idempotency-Key (обязательно),
    а также (опционально) client_nonce в теле запроса — дополнительный предохранитель.
    """

    amount_kwh: condecimal(gt=Decimal("0"), max_digits=30, decimal_places=10) = Field(
        ..., description="Положительное число kWh для обмена (> 0). Будет округлено вниз до 8 знаков."
    )
    client_nonce: Optional[constr(strip_whitespace=True, min_length=1, max_length=128)] = Field(
        None,
        description="Опциональное значение идемпотентности от клиента. НЕ заменяет Idempotency-Key в заголовке.",
    )

    # Приводим к канонам (8 знаков, вниз)
    @validator("amount_kwh", pre=True, always=True)
    def _q8_amount(cls, v: Decimal) -> Decimal:
        return d8(v)

    class Config:
        json_encoders = {Decimal: lambda v: str(d8(v))}


class ExchangeConvertOut(BaseModel):
    """
    Результат успешной конвертации.
    Все суммы — уже после округления/проведения через банковский сервис.
    """

    ok: bool = Field(True, description="Флаг успешной операции.")
    exchanged_kwh: Decimal = Field(..., description="Списано энергии, kWh (Decimal/8).")
    credited_efhc: Decimal = Field(..., description="Начислено EFHC (Decimal/8), по курсу 1:1.")
    available_kwh_after: Decimal = Field(
        ..., description="Остаток доступной энергии после обмена, kWh."
    )
    user_main_balance: Decimal = Field(
        ..., description="Текущий основной баланс EFHC пользователя."
    )
    user_bonus_balance: Decimal = Field(
        ..., description="Текущий бонусный баланс EFHC пользователя."
    )
    transfer_log_id: int = Field(
        ..., description="ID записи в efhc_transfers_log (зеркальная операция «пользователь↔банк»)."
    )
    idempotency_key: str = Field(
        ..., description="Фактически применённый идемпотентный ключ операции."
    )
    message: str = Field("", description="Подсказка для UI (например, «обмен выполнен успешно»).")
    processed_at: datetime = Field(..., description="Момент проведения операции на сервере (UTC).")

    # Квантизация Decimal-полей
    @validator(
        "exchanged_kwh",
        "credited_efhc",
        "available_kwh_after",
        "user_main_balance",
        "user_bonus_balance",
        pre=True,
        always=True,
    )
    def _q8_all(cls, v: Decimal) -> Decimal:
        return d8(v)

    class Config:
        json_encoders = {Decimal: lambda v: str(d8(v))}


# =============================================================================
# История обменов (лог витрины)
# =============================================================================


class ExchangeHistoryItemOut(BaseModel):
    """
    Элемент истории обменов пользователя.
    Источником выступают efhc_transfers_log и/или специализированные журналы обмена.
    """

    id: int = Field(..., description="Идентификатор записи в логе.")
    created_at: datetime = Field(..., description="Время записи (UTC).")
    direction: constr(strip_whitespace=True, min_length=1, max_length=16) = Field(
        ..., description="Направление записи (обычно 'credit' на пользователя)."
    )
    reason: constr(strip_whitespace=True, min_length=1, max_length=64) = Field(
        ..., description="Причина/тип операции (например, 'exchange_kwh_to_efhc')."
    )
    amount_kwh: Decimal = Field(
        ..., description="Списанная энергия (если ведётся отдельным полем), kWh."
    )
    amount_efhc: Decimal = Field(..., description="Начисленные EFHC, Decimal/8.")
    idempotency_key: Optional[str] = Field(
        None, description="Идемпотентный ключ, если применялся."
    )
    meta: Optional[str] = Field(None, description="Доп. сведения (например, источник вызова).")

    @validator("amount_kwh", "amount_efhc", pre=True, always=True)
    def _q8_amounts(cls, v: Decimal) -> Decimal:
        return d8(v)

    class Config:
        json_encoders = {Decimal: lambda v: str(d8(v))}


class ExchangeHistoryPage(CursorPage[ExchangeHistoryItemOut]):  # type: ignore[type-arg]
    """
    Страница истории обменов с курсорной пагинацией.
    Поля:
      • items: List[ExchangeHistoryItemOut]
      • next_cursor: Optional[str]
      • total_hint: Optional[int] — может заполняться сервисом/кэшем (не обязателен).
    """

    items: List[ExchangeHistoryItemOut]


# =============================================================================
# Пояснения «для чайника»:
# • Почему Decimal и почему 8 знаков?
#   Это канон проекта: финансовая/энергетическая точность фиксирована. Все значения
#   всегда квантуются «вниз» до 8 знаков — чтобы исключить «дорисованные» копейки.
#
# • Где здесь идемпотентность?
#   Схемы задают поле client_nonce (по желанию клиента), но настоящая защита — в
#   заголовке Idempotency-Key и банковском сервисе (UNIQUE индекс в логе).
#
# • Зачем предпросмотр?
#   Чтобы фронтенд не гадал, а показывал точные доступные суммы прямо сейчас, без
#   попытки пересчитать генерацию на клиенте (генерация/догон делаются бэкендом).
#
# • Почему нет обратной конверсии?
#   Запрещено каноном (kWh→EFHC строго 1:1, обратного пути нет).
# =============================================================================
