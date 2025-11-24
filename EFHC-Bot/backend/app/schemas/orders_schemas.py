# -*- coding: utf-8 -*-
# backend/app/schemas/orders_schemas.py
# =============================================================================
# Назначение кода:
# Pydantic-схемы для модуля «Заказы / Магазин (Shop Orders)» EFHC Bot.
# Этот файл описывает ВСЕ формы данных, которые роуты и сервисы используют
# при работе с заказами:
#   • выдача витрины «Мои заказы» (курсоры, без OFFSET),
#   • детальная карточка заказа,
#   • создание TON-инвойсов для покупки EFHC-пакетов и NFT (ручная модерация),
#   • внутренняя покупка витринных товаров за EFHC (GOOD) — списание через Банк,
#   • служебная «принудительная синхронизация» витрины при открытии экрана.
#
# Канон / инварианты (важно):
# • Денежные строки наружу — строго str с 8 знаками (Decimal, округление вниз).
# • Любой «денежный POST» обязан требовать заголовок Idempotency-Key.
#   Это обеспечивается наследованием входных моделей от IdempotencyContract.
# • Покупка EFHC-пакетов и NFT: оплата в TON → обработка через вотчер
#   (watcher_service) → списание/начисление ТОЛЬКО через банковский сервис.
# • NFT — только заявка и ручная выдача (никакой автодоставки NFT).
# • Внутренняя покупка товаров за EFHC (GOOD) списывает бонусы первыми,
#   затем основной баланс; пользователь НЕ может уйти в минус (жёсткий запрет).
# • P2P-переводы и обратная конверсия EFHC→kWh запрещены.
#
# ИИ-защита / самовосстановление:
# • Единая cursor-пагинация (CursorPage[T]) — для устойчивой подгрузки списков.
# • Служебные DTO (OkMeta, OrdersSyncOut) помогают фронтенду корректно
#   восстанавливаться после сбоев сети, повторов и т.п.
# • Все «денежные POST» декларируют требование идемпотентности на уровне схем.
#
# Запреты:
# • В схемах НЕТ бизнес-логики и пересчётов — только форма данных и валидация.
# • Никаких «суточных» абстракций: весь расчёт на стороне сервисов по канону.
# =============================================================================

from __future__ import annotations

# =============================================================================
# Импорты
# -----------------------------------------------------------------------------
from datetime import datetime
from typing import Any, Optional, Literal

from pydantic import BaseModel, Field, validator

from backend.app.schemas.common_schemas import (
    d8_str,            # форматирует Decimal → строка с 8 знаками (ROUND_DOWN)
    OkMeta,            # служебная «ок»-обёртка (ок/trace/ts) для устойчивости UI
    CursorPage,        # унифицированный контейнер курсорной пагинации
    BalancePair,       # пара балансов EFHC (main/bonus) строками с 8 знаками
    IdempotencyContract,  # базовый класс входных «денежных» запросов
)

# =============================================================================
# Типы и статусы заказов (синхронизация с моделями/миграциями)
# -----------------------------------------------------------------------------
OrderType = Literal["EFHC_PACKAGE", "NFT_VIP", "GOOD"]
"""
EFHC_PACKAGE — пакеты EFHC за TON (автодоставка через вотчер и Банк).
NFT_VIP      — заявка на NFT (оплата в TON, ТОЛЬКО ручная модерация админом).
GOOD         — обычный товар, покупка за EFHC внутри бота.
"""

OrderStatus = Literal[
    "PENDING",               # создан, ожидает оплаты/обработки
    "PAID_AUTO",             # оплачен автоматически (подтверждён вотчером по TON)
    "PAID_PENDING_MANUAL",   # оплачен TON, но требует ручной модерации (NFT)
    "APPROVED",              # завершён/доставлен (для GOODS/пакетов EFHC)
    "REJECTED",              # отклонён админом
    "CANCELLED"              # отменён по таймауту/пользователем
]

PaymentMode = Literal["TON", "EFHC"]
"""
TON  — оплата внешним переводом на кошелёк проекта (инвойс + MEMO),
EFHC — внутренняя покупка за EFHC (списание через банковский сервис).
"""

# =============================================================================
# Витрина «Мои заказы» (листинг с курсорами, без OFFSET)
# -----------------------------------------------------------------------------
class OrderRow(BaseModel):
    """
    Короткая строка заказа для таблицы/списка.
    Используется в CursorPage[OrderRow].
    """
    order_id: int = Field(..., description="ID заказа")
    user_id: int = Field(..., description="Внутренний ID пользователя")
    order_type: OrderType = Field(..., description="Тип заказа")
    title: str = Field(..., max_length=140, description="Заголовок/название позиции")
    status: OrderStatus = Field(..., description="Текущий статус заказа")
    payment_mode: PaymentMode = Field(..., description="Способ оплаты (TON/EFHC)")
    amount_ton: Optional[str] = Field(None, description="Сумма TON (str, 8) при оплате через TON")
    amount_efhc: Optional[str] = Field(None, description="Сумма EFHC (str, 8) при внутренней покупке")
    tx_hash: Optional[str] = Field(None, description="TON tx_hash, если применимо")
    created_at: datetime = Field(..., description="Когда создан (UTC)")
    updated_at: Optional[datetime] = Field(None, description="Когда обновлён (UTC)")

    @validator("amount_ton", "amount_efhc", pre=True)
    def _q8(cls, v: Any) -> Optional[str]:
        if v is None:
            return None
        return d8_str(v)

# Страница «Мои заказы» (унифицированный контейнер с items[], next_cursor, etag)
MyOrdersPage = CursorPage[OrderRow]

# =============================================================================
# Детальная карточка заказа (экран «Детали заказа»)
# -----------------------------------------------------------------------------
class OrderDetailsOut(BaseModel):
    """
    Развёрнутая карточка заказа для экрана деталей.
    Содержит платёжный контекст (to_address, memo) для TON-инвойса.
    """
    meta: OkMeta = Field(default_factory=OkMeta)
    order_id: int
    user_id: int
    order_type: OrderType
    title: str
    description: Optional[str]
    status: OrderStatus
    payment_mode: PaymentMode
    amount_ton: Optional[str]
    amount_efhc: Optional[str]
    tx_hash: Optional[str]
    memo: Optional[str] = Field(None, description="Ожидаемый MEMO (строгий парсинг вотчером)")
    to_address: Optional[str] = Field(None, description="TON-адрес проекта для оплаты")
    expires_at: Optional[datetime] = Field(None, description="Срок действия инвойса (если используется)")

    @validator("amount_ton", "amount_efhc", pre=True)
    def _q8(cls, v: Any) -> Optional[str]:
        if v is None:
            return None
        return d8_str(v)

# =============================================================================
# Создание TON-инвойса (EFHC-пакеты и NFT)
# -----------------------------------------------------------------------------
class TonInvoiceCreateIn(IdempotencyContract):
    """
    Входная модель для создания платёжного счёта в TON.
    Денежная операция → роут обязан требовать заголовок Idempotency-Key.

    item_id  — ID позиции в магазине (EFHC-пакет или NFT-позиция),
    quantity — количество для EFHC-пакетов (для NFT обычно 1).
    """
    item_id: int = Field(..., description="ID позиции в магазине (EFHC-пакет/NFT)")
    quantity: int = Field(..., ge=1, le=1000, description="Количество (для пакетов EFHC)")

class TonInvoiceCreateOut(BaseModel):
    """
    Результат создания TON-инвойса.
    Фронт показывает сумму, адрес и MEMO. Пользователь платит ровно эту сумму
    с этим MEMO. Вотчер (watcher_service) фиксирует вход и меняет статус заказа.
    """
    meta: OkMeta = Field(default_factory=OkMeta)
    order_id: int = Field(..., description="Созданный ID заказа")
    order_status: OrderStatus = Field(..., description="Ожидаемый статус (обычно PENDING)")
    to_address: str = Field(..., description="TON-адрес проекта для оплаты")
    amount_ton: str = Field(..., description="Сумма TON строкой (8 знаков)")
    memo: str = Field(..., description="Строгий MEMO (SKU:.../EFHC<tgid>) для идемпотентности")
    expires_at: Optional[datetime] = Field(None, description="Срок действия инвойса (если применяется)")

    @validator("amount_ton", pre=True)
    def _q8(cls, v: Any) -> str:
        return d8_str(v)

# =============================================================================
# Внутренняя покупка за EFHC (ТОЛЬКО GOODS по канону)
# -----------------------------------------------------------------------------
class EfhcOrderPurchaseIn(IdempotencyContract):
    """
    Внутренняя покупка витринного товара за EFHC.
    Денежная операция → заголовок Idempotency-Key обязателен.
    Списание делает банковский сервис: сначала бонусы, затем основной баланс.
    Пользователь не может уйти «в минус» — сервис отклонит операцию.
    """
    item_id: int = Field(..., description="ID позиции (тип GOOD)")
    quantity: int = Field(..., ge=1, le=1000, description="Количество (>0)")

class EfhcOrderPurchaseOut(BaseModel):
    """
    Результат внутренней покупки за EFHC.
    Возвращает списанную сумму и итоговые балансы пользователя.
    """
    meta: OkMeta = Field(default_factory=OkMeta)
    order_id: int = Field(..., description="ID созданного заказа")
    order_status: OrderStatus = Field(..., description="Статус заказа после списания")
    charged_efhc: str = Field(..., description="Сколько EFHC списано (str, 8)")
    balances_after: BalancePair = Field(..., description="Баланс пользователя (main/bonus) после операции")

    @validator("charged_efhc", pre=True)
    def _q8(cls, v: Any) -> str:
        return d8_str(v)

# =============================================================================
# Листинг «Мои заказы»: фильтры/курсоры
# -----------------------------------------------------------------------------
class MyOrdersQueryIn(BaseModel):
    """
    Параметры курсорной пагинации для «Мои заказы».
    • next_cursor — из предыдущего ответа; отсутствует для первой страницы.
    • limit — желаемый размер страницы; значение по умолчанию задаёт роут.
    • type/status — необязательные фильтры для UI.
    """
    next_cursor: Optional[str] = Field(None, description="Курсор следующей страницы (base64)")
    limit: Optional[int] = Field(None, ge=1, le=200, description="Размер страницы")
    type: Optional[OrderType] = Field(None, description="Фильтр по типу заказа")
    status: Optional[OrderStatus] = Field(None, description="Фильтр по статусу")

# =============================================================================
# Служебные DTO синхронизации витрины/заказов (на открытии экрана)
# -----------------------------------------------------------------------------
class OrdersSyncOut(BaseModel):
    """
    Результат «принудительной синхронизации» витрины/заказов.
    Фронт может вызвать этот роут при открытии раздела Shop/Orders, чтобы
    выровнять локальное состояние c сервером (догон, обновления статусов).
    """
    meta: OkMeta = Field(default_factory=OkMeta)
    refreshed: int = Field(..., ge=0, description="Сколько записей пересчитано/освежено")
    errors: int = Field(..., ge=0, description="Сколько ошибок во время синхронизации")
    note: Optional[str] = Field(None, description="Например: 'partial', 'rate_limited', 'ok'")

# =============================================================================
# Пояснения (для разработчиков/ревью):
# • Этот файл — только СХЕМЫ/ТИПЫ. Он не делает списаний и не меняет статусы.
#   Денежные операции выполняют сервисы через единый банковский модуль.
# • Входные модели «денежных POST» наследуют IdempotencyContract → роуты обязаны
#   проверять заголовок Idempotency-Key; иначе возвращать 400 (канон).
# • CursorPage[T] — общий контейнер: items[], next_cursor, etag.
#   Роуты должны кодировать курсор без OFFSET (например, по (created_at, id)).
# • Для TON-инвойса фронтенд получает to_address + amount_ton + memo, затем
#   watcher_service по tx_hash/MEMO идемпотентно завершает заказ.
# =============================================================================
