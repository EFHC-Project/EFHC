# -*- coding: utf-8 -*-
# backend/app/schemas/lotteries_schemas.py
# =============================================================================
# Назначение кода:
# Pydantic-схемы пользовательского раздела «Лотереи»: витрина активных лотерей,
# детали лотереи, покупка билетов (денежная операция, требует Idempotency-Key),
# мои билеты, публичные результаты розыгрышей и служебная синхронизация.
#
# Канон / инварианты:
# • Все денежные значения наружу — СТРОКОЙ с 8 знаками (Decimal, ROUND_DOWN).
# • Покупка билетов производится только за EFHC (бонусы расходуются первыми),
#   реальное движение средств выполняет банковский сервис (transactions_service).
# • Внутренний курс 1 EFHC = 1 kWh; обратной конверсии нет; P2P запрещён.
# • Пользователь НЕ может уйти в минус (жёсткий запрет на уровне сервисов).
#
# ИИ-защита:
# • Все «денежные POST» — через наследование IdempotencyContract: в роутере
#   обязателен заголовок Idempotency-Key; есть client_nonce для трассировки.
# • Курсорная пагинация через универсальный контейнер CursorPage[T].
# • Служебные ответы (OkMeta) помогают фронтенду корректно восстанавливаться.
#
# Запреты:
# • В схемах нет бизнес-логики/пересчётов, только форма данных.
# • Нет автодоставки NFT и иных запрещённых по канону операций.
# =============================================================================

from __future__ import annotations

# =============================================================================
# Импорты
# -----------------------------------------------------------------------------
from datetime import datetime
from typing import Any, Optional, List, Literal

from pydantic import BaseModel, Field, validator

from backend.app.schemas.common_schemas import (
    d8_str,
    OkMeta,
    CursorPage,
    BalancePair,
    IdempotencyContract,
)

# =============================================================================
# Карточки витрины и страницы (курсоры)
# -----------------------------------------------------------------------------
class LotteryCard(BaseModel):
    """
    Короткая карточка лотереи для витрины.
    """
    lottery_id: int = Field(..., description="ID лотереи")
    title: str = Field(..., max_length=140, description="Заголовок")
    description: Optional[str] = Field(None, max_length=2000, description="Краткое описание")
    ticket_price_efhc: str = Field(..., description="Цена билета (str, 8)")
    max_tickets_per_user: int = Field(..., ge=1, le=1000, description="Лимит билетов на пользователя")
    starts_at: Optional[datetime] = Field(None, description="Начало активности (UTC)")
    ends_at: Optional[datetime] = Field(None, description="Окончание активности (UTC)")
    is_active: bool = Field(..., description="Показывать в витрине сейчас?")
    sold_count: int = Field(..., ge=0, description="Сколько билетов продано")
    my_tickets_count: int = Field(..., ge=0, description="Сколько билетов купил текущий пользователь")

    @validator("ticket_price_efhc", pre=True)
    def _q8(cls, v: Any) -> str:
        return d8_str(v)


# Страница витрины (активные + при необходимости предстоящие/завершённые)
LotteryListPage = CursorPage[LotteryCard]


class LotteryDetailsOut(BaseModel):
    """
    Детальная карточка лотереи (для экрана лотереи).
    """
    meta: OkMeta = Field(default_factory=OkMeta)
    lottery_id: int
    title: str
    description: Optional[str]
    ticket_price_efhc: str
    max_tickets_per_user: int
    starts_at: Optional[datetime]
    ends_at: Optional[datetime]
    is_active: bool

    # Аггрегаты/контекст пользователя:
    sold_count: int
    my_tickets_count: int
    remaining_for_me: int = Field(..., ge=0, description="Сколько билетов ещё могу купить исходя из лимита")

    @validator("ticket_price_efhc", pre=True)
    def _q8(cls, v: Any) -> str:
        return d8_str(v)


# =============================================================================
# Покупка билетов (денежная операция) — вход/выход
# -----------------------------------------------------------------------------
class TicketPurchaseIn(IdempotencyContract):
    """
    Вход покупки лотерейных билетов.
    ВАЖНО: это денежная операция → в роутере обязателен заголовок Idempotency-Key.
    """
    lottery_id: int = Field(..., description="ID лотереи")
    quantity: int = Field(..., ge=1, le=1000, description="Сколько билетов купить (>0)")

class TicketPurchaseOut(BaseModel):
    """
    Результат покупки билетов.
    """
    meta: OkMeta = Field(default_factory=OkMeta)
    lottery_id: int = Field(..., description="ID лотереи")
    purchased: int = Field(..., ge=0, description="Сколько билетов куплено в этой операции")
    my_tickets_total: int = Field(..., ge=0, description="Сколько билетов всего у меня после операции")
    my_balances_after: BalancePair = Field(..., description="Итоговые балансы EFHC пользователя (main/bonus)")

# =============================================================================
# «Мои билеты» и публичные результаты
# -----------------------------------------------------------------------------
TicketStatus = Literal["ACTIVE", "WON", "LOST", "REFUNDED"]

class MyTicketCard(BaseModel):
    """
    Карточка моего билета для листинга.
    """
    ticket_id: int = Field(..., description="ID билета")
    lottery_id: int = Field(..., description="ID лотереи")
    number: Optional[str] = Field(None, description="Публичный номер билета (если предусмотрено)")
    purchased_at: datetime = Field(..., description="Когда куплен (UTC)")
    status: TicketStatus = Field(..., description="Статус билета")

# Страница «Мои билеты»
MyTicketsPage = CursorPage[MyTicketCard]


class DrawPublicResultOut(BaseModel):
    """
    Публичный результат розыгрыша (без раскрытия персональных данных).
    """
    meta: OkMeta = Field(default_factory=OkMeta)
    lottery_id: int
    drawn_at: Optional[datetime] = Field(None, description="Когда проведён розыгрыш (UTC)")
    winners_count: int = Field(..., ge=0, description="Сколько победителей")
    winning_ticket_ids: List[int] = Field(default_factory=list, description="Идентификаторы выигравших билетов")
    status: Literal["PLANNED", "RUNNING", "FINISHED", "CANCELLED"] = Field(
        ..., description="Состояние лотереи/розыгрыша"
    )
    note: Optional[str] = Field(None, description="Короткая служебная пометка")

# =============================================================================
# Служебные DTO для «принудительной синхронизации» экрана лотерей
# -----------------------------------------------------------------------------
class LotterySyncOut(BaseModel):
    """
    Результат синхронизации/освежения витрины лотерей.
    Фронт может вызывать при открытии экрана, чтобы выровнять состояние.
    """
    meta: OkMeta = Field(default_factory=OkMeta)
    refreshed: int = Field(..., ge=0, description="Сколько записей было пересчитано/обновлено")
    errors: int = Field(..., ge=0, description="Сколько ошибок возникло")
    note: Optional[str] = Field(None, description="Например, 'partial', 'rate_limited' и т.п.")

# =============================================================================
# Пояснения:
# • LotteryListPage / MyTicketsPage — это CursorPage[T] из common_schemas:
#   возвращают items[], next_cursor и etag; фронт должен передавать курсор
#   при подгрузке следующей страницы и учитывать ETag для кэш-валидации.
# • TicketPurchaseIn наследует IdempotencyContract → для POST покупки билетов
#   роутер обязан требовать заголовок Idempotency-Key (канон финансовой целостности).
# • Все деньги — строки с 8 знаками (d8_str). Списания делает банковский сервис:
#   бонусы списываются первыми, пользователь не может уйти «в минус».
# • DrawPublicResultOut — публичная агрегированная форма: никаких персональных
#   данных, только билет-ID и общие числа; детальные данные доступны админам.
# =============================================================================
