# -*- coding: utf-8 -*-
# backend/app/schemas/common_schemas.py
# =============================================================================
# Назначение кода:
# Базовые Pydantic-схемы EFHC Bot для всех API: курсорная пагинация,
# нормализация денежных/энергетических значений (Decimal с 8 знаками,
# округление вниз), типовые ответы/ошибки. Единый контракт для фронтенда.
#
# Канон / инварианты:
# • Все суммы и кВт·ч — Decimal(30, 8). Снаружи всегда выдаём строкой с 8 знаками.
# • Никаких «суточных» ставок — ставки только посекундные (канон проекта).
# • Все листинги в API используют курсорную пагинацию (без OFFSET/LIMIT для томов).
#
# ИИ-защиты:
# • Централизованная нормализация чисел в строки (ROUND_DOWN) защищает UI
#   от расхождений в форматах.
# • Единый контейнер CursorPage[...] с next_cursor и server_time — фронтенд
#   всегда понимает, как листать и диагностировать рассинхрон.
#
# Запреты:
# • Нет бизнес-логики и пересчётов — только декларативные DTO/валидаторы.
# • Нет альтернативных форматов (int/float) наружу — только str(Decimal(8)).
# =============================================================================

from __future__ import annotations

# =============================================================================
# Импорты
# -----------------------------------------------------------------------------
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN, InvalidOperation
from typing import Any, Generic, List, Optional, TypeVar

from pydantic import BaseModel, Field, validator
from pydantic.generics import GenericModel

# =============================================================================
# Точность и общие хелперы чисел
# -----------------------------------------------------------------------------
EFHC_DECIMALS: int = 8
_Q8 = Decimal(1).scaleb(-EFHC_DECIMALS)  # == Decimal("0.00000001")


def _to_decimal_q8(x: Any) -> Decimal:
    """
    Преобразует вход в Decimal и обрезает до 8 знаков (ROUND_DOWN).
    Бросает ValueError для некорректного ввода.
    """

    try:
        d = Decimal(str(x))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError("Некорректное числовое значение")
    return d.quantize(_Q8, rounding=ROUND_DOWN)


def d8_str(x: Any) -> str:
    """
    Унифицированная сериализация денежных/энергетических величин:
    приводим к Decimal(8, ROUND_DOWN) и возвращаем строку.
    """

    return str(_to_decimal_q8(x))


# =============================================================================
# Базовые ответы/ошибки
# -----------------------------------------------------------------------------
class ErrorResponse(BaseModel):
    """Стандартная форма ошибки для фронтенда."""

    code: str = Field(..., description="Короткий код ошибки (snake-case)")
    detail: str = Field(..., description="Человеко-читаемое описание проблемы")


class OkMeta(BaseModel):
    """Мини-мета об успешной обработке."""

    ok: bool = Field(True, description="Флаг успешной операции")
    server_time: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
        description="UTC-время формирования ответа (ISO-8601)",
    )


# =============================================================================
# Cursor-based пагинация (без OFFSET)
# -----------------------------------------------------------------------------
T = TypeVar("T")


class CursorPage(GenericModel, Generic[T]):
    """
    Контейнер страницы списка:
      • items — элементы текущей выборки;
      • next_cursor — курсор следующей страницы (или None);
      • etag — опциональный хэш содержимого страницы;
      • server_time — отметка времени формирования ответа.
    """

    items: List[T] = Field(..., description="Элементы текущей страницы")
    next_cursor: Optional[str] = Field(None, description="Курсор следующей страницы или None")
    etag: Optional[str] = Field(None, description="Опциональный ETag ответа")
    server_time: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
        description="UTC-время формирования ответа (ISO-8601)",
    )


# =============================================================================
# Строгие «строки-числа» (денежные/энергетические)
# -----------------------------------------------------------------------------
class MoneyStr(BaseModel):
    """
    Денежное значение EFHC, сериализованное как строка с 8 знаками.
    В БД — NUMERIC(30, 8); наружу — только str.
    """

    value: str = Field(..., description="Строка с 8 знаками после запятой")

    @validator("value", pre=True)
    def _norm(cls, v: Any) -> str:
        return d8_str(v)


class EnergyStr(BaseModel):
    """
    Энергетическое значение (кВт·ч), сериализованное как строка с 8 знаками.
    """

    value: str = Field(..., description="КВт·ч строкой с 8 знаками")

    @validator("value", pre=True)
    def _norm(cls, v: Any) -> str:
        return d8_str(v)


# =============================================================================
# Частые DTO-пары (балансы/энергия) и флаги
# -----------------------------------------------------------------------------
class BalancePair(BaseModel):
    """
    Пара балансов пользователя: основной и бонусный (строки с 8 знаками).
    """

    main_balance: str = Field(..., description="Основной баланс EFHC (str, 8 знаков)")
    bonus_balance: str = Field(..., description="Бонусны баланс EFHC (str, 8 знаков)")

    @validator("main_balance", "bonus_balance", pre=True)
    def _norm(cls, v: Any) -> str:
        return d8_str(v)


class EnergyPair(BaseModel):
    """
    Раздельный учёт энергии (канон):
      • total_generated_kwh — для рейтинга/достижений (не тратится),
      • available_kwh — для обмена (уменьшается при конвертации).
    """

    total_generated_kwh: str = Field(..., description="Всего сгенерировано (кВт·ч), str(8)")
    available_kwh: str = Field(..., description="Доступно для обмена (кВт·ч), str(8)")

    @validator("total_generated_kwh", "available_kwh", pre=True)
    def _norm(cls, v: Any) -> str:
        return d8_str(v)


class VipFlags(BaseModel):
    """Мини-набор флагов статуса пользователя."""

    is_vip: bool = Field(..., description="VIP по наличию NFT из коллекции")


# =============================================================================
# Идемпотентность денежных операций
# -----------------------------------------------------------------------------
class IdempotencyContract(BaseModel):
    """
    Контракт идемпотентности для денежных POST:
      • заголовок Idempotency-Key обязателен (проверяется в роутере),
      • client_nonce — опциональный нонс для трассировки.
    """

    client_nonce: Optional[str] = Field(
        None, description="Опциональный клиентский нонс для трассировки"
    )


# =============================================================================
# Универсальные карточки витрин (пример: панели, предпросмотр обмена)
# -----------------------------------------------------------------------------
class PanelCard(BaseModel):
    """
    Карточка панели (активной или архивной). Даты — ISO-8601,
    числовые значения — строки с 8 знаками.
    """

    panel_id: int = Field(..., description="ID панели")
    is_active: bool = Field(..., description="Активность (истёк срок — False)")
    created_at: str = Field(..., description="ISO-время создания")
    expires_at: Optional[str] = Field(None, description="ISO-время истечения (для активной)")
    closed_at: Optional[str] = Field(None, description="ISO-время закрытия (для архивной)")
    base_gen_per_sec: str = Field(..., description="Ставка генерации, кВт⋅ч/сек (str, 8)")
    generated_kwh: str = Field(..., description="Сгенерировано этой панелью, кВт⋅ч (str, 8)")

    @validator("base_gen_per_sec", "generated_kwh", pre=True)
    def _norm_decimals(cls, v: Any) -> str:
        return d8_str(v)


class ExchangePreviewDTO(BaseModel):
    """
    Предпросмотр обмена kWh→EFHC (ничего не списывает).
    """

    ok: bool = Field(..., description="Можно ли выполнить обмен прямо сейчас")
    available_kwh: str = Field(..., description="Доступная энергия (кВт⋅ч), str(8)")
    max_exchangeable_kwh: str = Field(
        ..., description="Максимум, который можно обменять сейчас, кВт⋅ч, str(8)"
    )
    rate_kwh_to_efhc: str = Field(
        ..., description="Фиксированный курс 1.00000000"
    )
    detail: str = Field(..., description="Человеко-читаемая подсказка/причина")


# =============================================================================
# Пояснения:
# • Этот модуль — слой представления. Никаких операций с БД/балансами.
# • Все числа наружу — только через d8_str(): единый формат "0.00000000".
# • CursorPage[T] — единый контракт листингов (next_cursor + etag + server_time).
# • Профильные схемы (user/panels/exchange/shop/…) импортируют DTO отсюда, чтобы
#   исключить дублирование и разъезд форм ответов.
# =============================================================================
