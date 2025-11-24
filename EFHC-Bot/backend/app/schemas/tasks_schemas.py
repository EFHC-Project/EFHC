# -*- coding: utf-8 -*-
# backend/app/schemas/tasks_schemas.py
# =============================================================================
# Назначение кода:
# Pydantic-схемы для модуля «Задания (Tasks)» и «Отправки выполнения (Submissions)».
# Эти схемы покрывают:
#   • админ-операции (создание/редактирование заданий, модерация отправок),
#   • пользовательские операции (листинг активных заданий, отправка выполнения),
#   • курсорную пагинацию без OFFSET и дружелюбную синхронизацию UI.
#
# Канон/инварианты (важно):
# • Денежные значения наружу — только строками с 8 знаками (Decimal, округление вниз).
# • Любой «денежный POST» обязан требовать Idempotency-Key (через наследование
#   входной модели от IdempotencyContract).
# • Начисление бонусов за задания выполняется ТОЛЬКО через банковский сервис.
# • P2P и обратные конверсии запрещены (в этих схемах таких действий нет).
#
# ИИ-защита/самовосстановление:
# • Единый контейнер CursorPage[T] для устойчивой подгрузки списков (next_cursor, etag).
# • OkMeta в комплексных ответах для дружественного восстановления UI после сбоёв.
#
# Запреты:
# • Схемы НЕ содержат бизнес-логики и пересчётов — только форма данных и валидация.
# • Никаких «суточных» абстракций — расчёты выполняют сервисы по per-sec канону.
# =============================================================================

from __future__ import annotations

# =============================================================================
# Импорты
# -----------------------------------------------------------------------------
from datetime import datetime
from decimal import Decimal
from typing import Any, List, Optional

from pydantic import BaseModel, Field, HttpUrl, conint, constr, validator

from backend.app.schemas.common_schemas import (
    OkMeta,             # ok/trace/ts для дружелюбных ответов
    CursorPage,         # унифицированный контейнер курсорной пагинации
    IdempotencyContract,# базовый контракт для денежных POST (Idempotency-Key обязателен)
    d8_str,             # Decimal/число → строка с 8 знаками (ROUND_DOWN)
)

# =============================================================================
# ЗАДАНИЯ: входные модели для админ-CRUD
# -----------------------------------------------------------------------------
class TaskBase(BaseModel):
    """Базовое описание задания (используется в create/update)."""
    code: constr(strip_whitespace=True, min_length=1, max_length=64)
    title: constr(strip_whitespace=True, min_length=1, max_length=255)
    description: Optional[str] = None
    type: constr(strip_whitespace=True, min_length=1, max_length=50) = "custom"
    active: bool = True

    # ВХОД (админ задаёт числа); ВЫХОД будет строкой с 8 знаками в витринах
    reward_bonus_efhc: Decimal = Field(Decimal("0.00000000"))
    price_usd_hint: Optional[Decimal] = None

    limit_per_user: conint(ge=0) = 1
    total_limit: Optional[conint(ge=0)] = None

    # Требования к пруфу выполнения
    proof_type: Optional[constr(strip_whitespace=True, max_length=50)] = None
    proof_hint: Optional[constr(strip_whitespace=True, max_length=255)] = None


class TaskCreate(TaskBase):
    """Создание задания (админ)."""
    pass


class TaskUpdate(BaseModel):
    """Изменение задания (админ). Все поля опциональны."""
    title: Optional[constr(strip_whitespace=True, min_length=1, max_length=255)] = None
    description: Optional[str] = None
    type: Optional[constr(strip_whitespace=True, min_length=1, max_length=50)] = None
    active: Optional[bool] = None
    reward_bonus_efhc: Optional[Decimal] = None
    price_usd_hint: Optional[Decimal] = None
    limit_per_user: Optional[conint(ge=0)] = None
    total_limit: Optional[conint(ge=0)] = None
    proof_type: Optional[constr(strip_whitespace=True, max_length=50)] = None
    proof_hint: Optional[constr(strip_whitespace=True, max_length=255)] = None


# =============================================================================
# ЗАДАНИЯ: витринные DTO (наружу — строки с 8 знаками)
# -----------------------------------------------------------------------------
class TaskOut(BaseModel):
    """
    Витринная карточка задания (для списков/деталей).
    Денежные значения → строками с 8 знаками (канон).
    """
    id: int
    code: str
    title: str
    description: Optional[str] = None
    type: str
    active: bool

    reward_bonus_efhc: str
    price_usd_hint: Optional[str] = None

    limit_per_user: int
    total_limit: Optional[int] = None

    performed_count: int
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True

    # Преобразуем Decimal → str(8)
    @validator("reward_bonus_efhc", "price_usd_hint", pre=True)
    def _q8(cls, v: Any) -> Optional[str]:
        if v is None:
            return None
        return d8_str(v)


# Страница задач (курсоры без OFFSET)
TasksPage = CursorPage[TaskOut]


# =============================================================================
# ЗАДАНИЯ: параметры курсорного листинга
# -----------------------------------------------------------------------------
class TasksQueryIn(BaseModel):
    """
    Параметры листинга задач:
      • next_cursor — курсор из предыдущей страницы;
      • limit — желаемый размер страницы;
      • only_active — фильтр «только активные».
    """
    next_cursor: Optional[str] = Field(None, description="Курсор следующей страницы (base64)")
    limit: Optional[int] = Field(None, ge=1, le=200, description="Размер страницы (1..200)")
    only_active: Optional[bool] = Field(None, description="Если true — только активные задания")


# =============================================================================
# ОТПРАВКИ ВЫПОЛНЕНИЯ (Submissions)
# -----------------------------------------------------------------------------
class SubmissionCreate(BaseModel):
    """
    Пользователь отправляет выполнение задания.
    Как минимум одно из полей proof_text/proof_url должно присутствовать,
    в зависимости от настроек задания (proof_type/proof_hint).
    """
    proof_text: Optional[str] = None
    proof_url: Optional[HttpUrl] = None


class SubmissionOut(BaseModel):
    """
    Витринная карточка отправки (для списков/деталей).
    reward_amount_efhc — строка с 8 знаками (какая сумма будет/была выплачена).
    """
    id: int
    task_id: int
    user_tg_id: int
    status: str  # PENDING / APPROVED / REJECTED / CLAIMING / CLAIMED …
    proof_text: Optional[str] = None
    proof_url: Optional[str] = None

    reward_amount_efhc: str
    paid: bool
    paid_tx_id: Optional[int] = None
    moderator_tg_id: Optional[int] = None

    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True

    @validator("reward_amount_efhc", pre=True)
    def _q8(cls, v: Any) -> str:
        return d8_str(v)


# Страница отправок (курсоры без OFFSET)
SubmissionsPage = CursorPage[SubmissionOut]


class SubmissionsQueryIn(BaseModel):
    """
    Параметры листинга отправок:
      • next_cursor — курсор из предыдущей страницы;
      • limit — желаемый размер страницы;
      • status — необязательный фильтр по статусу.
    """
    next_cursor: Optional[str] = Field(None, description="Курсор следующей страницы (base64)")
    limit: Optional[int] = Field(None, ge=1, le=200, description="Размер страницы (1..200)")
    status: Optional[constr(strip_whitespace=True, min_length=1, max_length=32)] = None


# =============================================================================
# МОДЕРАЦИЯ (админ): денежный POST → требуем Idempotency-Key
# -----------------------------------------------------------------------------
class SubmissionModerateIn(IdempotencyContract):
    """
    Админ/модератор принимает решение по заявке.
    • approve=True  → будет денежная выплата бонусов EFHC (через банковский сервис).
    • approve=False → заявка отклоняется (денежной операции нет).
    Денежный POST → обязательный Idempotency-Key (наследуем IdempotencyContract).
    """
    approve: bool
    moderator_tg_id: int
    # Админ может переопределить сумму выплаты; иначе берётся reward из задания.
    reward_override_efhc: Optional[Decimal] = None


# =============================================================================
# СЛУЖЕБНЫЕ DTO (синхронизация/диагностика)
# -----------------------------------------------------------------------------
class TasksSyncOut(BaseModel):
    """
    Результат «принудительной синхронизации» витрины задач
    (фронтенд может вызывать при открытии раздела).
    """
    meta: OkMeta = Field(default_factory=OkMeta)
    refreshed: int = Field(..., ge=0, description="Сколько записей освежено/пересчитано")
    errors: int = Field(..., ge=0, description="Сколько ошибок за проход")
    note: Optional[str] = Field(None, description="Короткий комментарий ('ok'/'partial')")


class SubmissionsSyncOut(BaseModel):
    """
    Результат «принудительной синхронизации» витрины отправок.
    """
    meta: OkMeta = Field(default_factory=OkMeta)
    refreshed: int = Field(..., ge=0)
    errors: int = Field(..., ge=0)
    note: Optional[str] = Field(None)


# =============================================================================
# Пояснения (для разработчиков/ревью):
# • TasksPage/SubmissionsPage — это CursorPage[T]: items[], next_cursor, etag.
#   Роуты ДОЛЖНЫ кодировать курсор без OFFSET (например, по (created_at, id)).
# • Входные модели с денежным эффектом наследуют IdempotencyContract:
#   роут обязан проверять заголовок Idempotency-Key и возвращать 400 при его отсутствии.
# • reward_bonus_efhc/price_usd_hint и reward_amount_efhc в OUT-моделях — строки с 8 знаками.
# • Все начисления выполняются через банковский сервис; схемы только описывают форму данных.
# =============================================================================
