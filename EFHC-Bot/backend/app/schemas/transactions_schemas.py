# -*- coding: utf-8 -*-
# backend/app/schemas/transactions_schemas.py
# =============================================================================
# Назначение кода:
# Pydantic-схемы для денежных операций EFHC Bot и журнала переводов.
# Схемы покрывают:
#   • входные модели «денежных POST» (обязательно с Idempotency-Key),
#   • витринные карточки лога переводов efhc_transfers_log,
#   • курсорную пагинацию без OFFSET для списков,
#   • служебные ответы «принудительной синхронизации» (догон/восстановление UI).
#
# Канон / инварианты:
# • Единственный источник движения EFHC — центральный Банк (bank ↔ user).
# • Разрешён только обмен kWh→EFHC 1:1; обратной конверсии НЕТ.
# • P2P user→user запрещён (в этих схемах таких операций нет).
# • Пользователь НЕ может уходить в минус (жёстко в коде). Банк может.
# • Все суммы наружу — СТРОКА с 8 знаками (Decimal, округление вниз).
# • Любой денежный POST обязан требовать Idempotency-Key.
#
# ИИ-защита / самовосстановление:
# • CursorPage[T] с next_cursor и etag, OkMeta в комплексных ответах.
# • Входные модели денежных операций наследуют IdempotencyContract.
#
# Запреты:
# • Никакой бизнес-логики/перерасчётов в схемах — только форма данных/валидация.
# • Никаких «суточных» ставок; это не относится к транзакциям.
# =============================================================================

from __future__ import annotations

# =============================================================================
# Импорты
# -----------------------------------------------------------------------------
from datetime import datetime
from decimal import Decimal
from typing import Any, Optional, Literal, Dict

from pydantic import BaseModel, Field, conint, constr, validator

from backend.app.schemas.common_schemas import (
    OkMeta,              # ok/trace/ts — дружелюбие к UI
    CursorPage,          # единый контейнер курсорной пагинации
    IdempotencyContract, # базовый контракт для «денежных POST» (Idempotency-Key обязателен)
    d8_str,              # Decimal/число → строка с 8 знаками (ROUND_DOWN)
)

# Типы перечислений (как строки) — соответствуют колонкам в efhc_transfers_log
BalanceType = Literal["main", "bonus"]
Direction = Literal["credit", "debit"]

# Возможные «reason» (не жёсткое перечисление, но подсказываем витрине/админке)
# Примеры: 'emission', 'burn', 'exchange_kwh_to_efhc', 'panel_purchase',
#          'task_bonus', 'shop_auto', 'withdraw', 'admin_fix'
ReasonStr = constr(strip_whitespace=True, min_length=1, max_length=64)

# =============================================================================
# Витринная карточка записи efhc_transfers_log
# -----------------------------------------------------------------------------
class TransferLogRow(BaseModel):
    """
    Витрина одной записи из efhc_transfers_log.
    """
    id: int = Field(..., description="Первичный ключ записи лога")
    user_id: Optional[int] = Field(None, description="ID пользователя (если применимо)")
    telegram_id: Optional[int] = Field(None, description="Telegram ID пользователя (для удобства витрины)")
    amount_efhc: str = Field(..., description="Сумма операции (str, 8)")
    direction: Direction = Field(..., description="Направление: credit=банк→пользователь, debit=пользователь→банк")
    balance_type: BalanceType = Field(..., description="main | bonus — какой баланс пользователя затронут")
    reason: str = Field(..., description="Причина/контекст операции (см. примеры)")
    idempotency_key: str = Field(..., description="Уникальный ключ идемпотентности")
    processed_with_bank_deficit: bool = Field(
        False, description="True, если операция прошла в режиме дефицита банка"
    )
    extra_info: Optional[Dict[str, Any]] = Field(
        None, description="Произвольные витринные метаданные (например, SKU/TX)"
    )
    created_at: datetime = Field(..., description="Момент фиксации операции")

    class Config:
        from_attributes = True

    @validator("amount_efhc", pre=True)
    def _q8(cls, v: Any) -> str:
        return d8_str(v)

# Страница журнала переводов (курсоры без OFFSET)
TransfersPage = CursorPage[TransferLogRow]


# =============================================================================
# Универсальные входные модели «денежных POST»
# -----------------------------------------------------------------------------
class AdminCreditUserIn(IdempotencyContract):
    """
    Эмиссия из Банка пользователю (admin → bank_service.credit_user_from_bank).
    Денежная операция — обязателен Idempotency-Key.
    """
    # идентификация пользователя (один из вариантов обязателен на уровне роута)
    user_id: Optional[int] = Field(None, description="Внутренний ID пользователя")
    telegram_id: Optional[int] = Field(None, description="Telegram ID пользователя")
    # сумма эмиссии
    amount_efhc: Decimal = Field(..., description="Сумма EFHC для начисления пользователю")
    reason: ReasonStr = Field("emission", description="Причина (по умолчанию 'emission')")
    note: Optional[str] = Field(None, description="Короткая приметка для журнала/админки")


class AdminDebitUserIn(IdempotencyContract):
    """
    Списание у пользователя в Банк (admin → bank_service.debit_user_to_bank).
    Денежная операция — обязателен Idempotency-Key.
    """
    user_id: Optional[int] = Field(None, description="Внутренний ID пользователя")
    telegram_id: Optional[int] = Field(None, description="Telegram ID пользователя")
    amount_efhc: Decimal = Field(..., description="Сумма EFHC для списания в Банк")
    balance_type: BalanceType = Field("main", description="С какого баланса списывать (main|bonus)")
    reason: ReasonStr = Field("burn", description="Причина (по умолчанию 'burn')")
    note: Optional[str] = Field(None, description="Короткая приметка для журнала/админки")


class ExchangeKwhToEfhsIn(IdempotencyContract):
    """
    Пользовательская конвертация энергии → EFHC (exchange_service.exchange_kwh_to_efhc).
    Денежная операция — обязателен Idempotency-Key.
    """
    user_id: Optional[int] = Field(None, description="Внутренний ID пользователя (если не берём из токена)")
    telegram_id: Optional[int] = Field(None, description="Telegram ID пользователя (fallback)")
    amount_kwh: Optional[Decimal] = Field(
        None, description="Сколько kWh обменять на EFHC 1:1. Если None и exchange_all=True — обменять всё доступное."
    )
    exchange_all: Optional[bool] = Field(
        False, description="Если True — игнорируем amount_kwh и обмениваем всё доступное"
    )
    # reason пусть фиксируется сервисом как 'exchange_kwh_to_efhc'


# =============================================================================
# Результаты денежных операций (витрина ответа)
# -----------------------------------------------------------------------------
class TransactionResultOut(BaseModel):
    """
    Результат денежной операции (эмиссия/списание/обмен).
    Возвращает ключевые поля для UI + новые балансы пользователя.
    """
    meta: OkMeta = Field(default_factory=OkMeta)
    log_id: int = Field(..., description="ID записи в efhc_transfers_log (зеркальная сторона операции)")
    idempotency_key: str = Field(..., description="Эхо присланного ключа идемпотентности")
    direction: Direction
    balance_type: BalanceType
    reason: str
    amount_efhc: str = Field(..., description="Сумма операции (str, 8)")

    processed_with_bank_deficit: bool = Field(
        False, description="True, если операция прошла при отрицательном балансе банка"
    )

    # Сводка обновлённых балансов пользователя (строки с 8 знаками)
    user_id: int
    telegram_id: Optional[int] = None
    main_balance: str
    bonus_balance: str
    available_kwh: str
    total_generated_kwh: str
    created_at: datetime

    @validator("amount_efhc", "main_balance", "bonus_balance",
               "available_kwh", "total_generated_kwh", pre=True)
    def _q8(cls, v: Any) -> str:
        return d8_str(v)


# =============================================================================
# Запросы на листинг журнала переводов (курсоры без OFFSET)
# -----------------------------------------------------------------------------
class TransfersQueryIn(BaseModel):
    """
    Фильтры и курсоры для листинга efhc_transfers_log.
    Используется в пользовательских и админских роутах (админ может задавать user_id).
    """
    # курсорная пагинация
    next_cursor: Optional[str] = Field(None, description="Курсор следующей страницы (base64)")
    limit: Optional[conint(ge=1, le=200)] = Field(None, description="Размер страницы (1..200)")

    # необязательные фильтры
    user_id: Optional[int] = Field(None, description="Фильтр по пользователю (только для админа)")
    direction: Optional[Direction] = Field(None, description="credit | debit")
    balance_type: Optional[BalanceType] = Field(None, description="main | bonus")
    reason: Optional[str] = Field(None, description="Подстрока/точное совпадение причины")
    idempotency_key: Optional[str] = Field(None, description="Фильтр по ключу идемпотентности")
    date_from: Optional[datetime] = Field(None, description="Нижняя граница created_at")
    date_to: Optional[datetime] = Field(None, description="Верхняя граница created_at")


# Готовая страница журнала
TransfersListOut = TransfersPage


# =============================================================================
# Служебные DTO: «принудительная синхронизация»/диагностика
# -----------------------------------------------------------------------------
class TransactionsSyncOut(BaseModel):
    """
    Результат служебной синхронизации (например, догон неподтверждённых операций).
    """
    meta: OkMeta = Field(default_factory=OkMeta)
    refreshed: int = Field(..., ge=0, description="Сколько записей/балансов освежено")
    errors: int = Field(..., ge=0, description="Сколько ошибок за проход")
    note: Optional[str] = Field(None, description="Короткая приметка ('ok'/'partial')")


# =============================================================================
# Пояснения (для разработчиков/ревью):
# • Входные денежные модели наследуют IdempotencyContract — роут обязан проверять
#   наличие заголовка Idempotency-Key и возвращать 400 при его отсутствии.
# • TransactionResultOut возвращает зеркальную запись лога, «эхо» ключа
#   идемпотентности и обновлённые балансы пользователя строками (8 знаков).
# • TransfersQueryIn + TransfersPage обеспечивают курсорную пагинацию без OFFSET.
# • P2P и обратная конверсия в этих схемах отсутствуют — запрещено каноном.
# =============================================================================
