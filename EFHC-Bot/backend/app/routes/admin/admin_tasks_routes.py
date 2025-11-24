# -*- coding: utf-8 -*-
# backend/app/routes/admin_tasks_routes.py
# =============================================================================
# Назначение кода:
#   Админские HTTP-ручки для блока «Задания» (Tasks) в EFHC Bot:
#   • управлять списком заданий (создать/обновить/получить/удалить),
#   • просматривать пользовательские заявки (submit/proof) с курсорной пагинацией,
#   • МОДЕРИРОВАТЬ заявки (approve/reject) с безопасной выплатой бонусов EFHC
#     ЧЕРЕЗ единый банковский сервис (строгая идемпотентность по Idempotency-Key).
#
# Зачем отдельный файл:
#   Это «админский фасад» для веб-админки/панели модерации: фронтенд админки
#   вызывает эти ручки, а не бизнес-сервисы напрямую. Здесь:
#     • проверяется админ-доступ,
#     • валидируются входные данные,
#     • исполняются канонические правила API (Idempotency-Key, ETag, курсоры),
#     • а ВСЕ денежные действия делегируются в tasks_service → transactions_service.
#
# Канон/инварианты (важно):
#   • Любая денежная POST-операция (выплата за задание) ОБЯЗАТЕЛЬНО требует
#     заголовок Idempotency-Key. Повторы с тем же ключом не создают дублей.
#   • Деньги двигает ТОЛЬКО банк (transactions_service). В этом файле нет прямых
#     SQL на балансы и не будет — только оркестрация.
#   • Списки всегда выдаём с курсорной пагинацией (без OFFSET) + ETag для кэша.
#   • Никакой P2P, никакой «скрытой эмиссии». VIP/суточные значения не считаем здесь.
#
# ИИ-защита/самовосстановление:
#   • «Мягкие» импорты сервисов: если сервис временно недоступен/отсутствует —
#     возвращаем 503 и НЕ «роняем» процесс; клиент может безопасно ретраить.
#   • Валидация денег через Decimal(8) (округление вниз), чтобы исключить дрейф.
#   • Чёткие статусы и сообщения об ошибках — админ понимает, что делать дальше.
#
# Запреты:
#   • Нет прямых обновлений денежных балансов.
#   • Нет auto-approve c выплатой без Idempotency-Key.
#   • Нет OFFSET/LIMIT пагинации — только курсоры из deps.encode_cursor/decode_cursor.
#
# Как использует фронтенд админки (пример):
#   1) Список заданий:   GET /api/admin/tasks?limit=50&cursor=<...>  → { items, next_cursor, ETag }
#   2) Создать задание:  POST /api/admin/tasks  (JSON body без денег) → 201 + TaskOut
#   3) Заявки по задаче: GET /api/admin/tasks/{task_id}/subs?status=PENDING&limit=50
#   4) Модерация заявки: POST /api/admin/submissions/{id}/moderate
#        Headers: Idempotency-Key: <uuid v4>
#        Body:    {"approve": true, "reward_override_efhc": "0.50000000"}
#      → Выплата бонуса пойдёт ТОЛЬКО через банковский сервис, идемпотентно.
# =============================================================================

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status
from pydantic import BaseModel, Field, conint, constr, validator
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.deps import (
    get_db,             # AsyncSession провайдер
    require_admin,      # 403 если не админ
    d8,                 # Decimal(8) округление вниз
    encode_cursor,      # dict -> base64url cursor
    decode_cursor,      # base64url cursor -> dict
    etag,               # хэш тела ответа для ETag
)
from backend.app.core.logging_core import get_logger

logger = get_logger(__name__)
router = APIRouter(prefix="/api/admin", tags=["admin:tasks"])

# -----------------------------------------------------------------------------
# «Мягкие» импорты сервисов (если сервис не готов — роутер отвечает 503, не падаем)
# -----------------------------------------------------------------------------
try:
    # Контракты сервисов (минимальный набор), реализованы в backend/app/services/tasks_service.py:
    #   async def admin_create_task(db, data: dict) -> dict
    #   async def admin_update_task(db, task_id: int, data: dict) -> dict|None
    #   async def admin_delete_task(db, task_id: int) -> bool
    #   async def admin_get_task(db, task_id: int) -> dict|None
    #   async def admin_list_tasks(db, limit: int, after: dict|None, q: str|None, active: bool|None) -> dict
    #       returns {"items":[...], "next_after": {...}|None}
    #   async def admin_list_submissions(db, task_id: int, limit: int, after: dict|None, status: str|None) -> dict
    #   async def moderate_submission_and_pay(db, submission_id: int, approve: bool,
    #                                         moderator_tg_id: int|None,
    #                                         reward_override_efhc: Decimal|None,
    #                                         idempotency_key: str) -> dict
    from backend.app.services.tasks_service import (  # type: ignore
        admin_create_task,
        admin_update_task,
        admin_delete_task,
        admin_get_task,
        admin_list_tasks,
        admin_list_submissions,
        moderate_submission_and_pay,
    )
    _TASK_SVC_AVAILABLE = True
except Exception:
    admin_create_task = admin_update_task = admin_delete_task = None
    admin_get_task = admin_list_tasks = admin_list_submissions = None
    moderate_submission_and_pay = None
    _TASK_SVC_AVAILABLE = False

# -----------------------------------------------------------------------------
# Pydantic-схемы ответов/входа (локально, чтобы не тянуть app/schemas до их готовности)
# -----------------------------------------------------------------------------

class TaskCreate(BaseModel):
    """Создание задания (без денежных эффектов)."""
    title: constr(strip_whitespace=True, min_length=1, max_length=200)
    description: Optional[str] = None
    is_active: bool = True
    reward_efhc: constr(strip_whitespace=True) = Field(..., description="Номинальная награда EFHC (строка Decimal(30,8))")
    kind: constr(strip_whitespace=True, min_length=1, max_length=50) = "generic"
    meta: Optional[Dict[str, Any]] = None

    @validator("reward_efhc")
    def _v_reward(cls, v: str) -> str:
        amt = d8(v)
        if amt <= Decimal("0"):
            raise ValueError("reward_efhc должен быть > 0")
        return str(amt)

class TaskUpdate(BaseModel):
    """Частичное редактирование задания (без немедленной выплаты)."""
    title: Optional[constr(strip_whitespace=True, min_length=1, max_length=200)] = None
    description: Optional[str] = None
    is_active: Optional[bool] = None
    reward_efhc: Optional[constr(strip_whitespace=True)] = None
    kind: Optional[constr(strip_whitespace=True, min_length=1, max_length=50)] = None
    meta: Optional[Dict[str, Any]] = None

    @validator("reward_efhc")
    def _v_reward(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        amt = d8(v)
        if amt <= Decimal("0"):
            raise ValueError("reward_efhc должен быть > 0")
        return str(amt)

class TaskOut(BaseModel):
    """DTO задания для админ-витрин."""
    id: int
    title: str
    description: Optional[str] = None
    is_active: bool
    reward_efhc: str
    kind: str
    meta: Optional[Dict[str, Any]] = None
    created_at: datetime
    updated_at: datetime

class PaginatedTasks(BaseModel):
    """Ответ со списком заданий (курсоры)."""
    items: List[TaskOut]
    next_cursor: Optional[str] = None

class SubmissionOut(BaseModel):
    """DTO пользовательской заявки на задание."""
    id: int
    task_id: int
    user_id: int
    status: str
    proof_url: Optional[str] = None
    submitted_at: datetime
    reviewed_at: Optional[datetime] = None
    reward_efhc: Optional[str] = None
    moderator_tg_id: Optional[int] = None

class PaginatedSubmissions(BaseModel):
    """Ответ со списком заявок (курсоры)."""
    items: List[SubmissionOut]
    next_cursor: Optional[str] = None

class SubmissionModerateIn(BaseModel):
    """Вход модерации: approve/reject (+опциональная ручная сумма)."""
    approve: bool
    moderator_tg_id: Optional[int] = Field(None, description="Telegram ID модератора для аудита")
    reward_override_efhc: Optional[constr(strip_whitespace=True)] = Field(
        None, description="Необязательная ручная сумма награды (строка Decimal(30,8))"
    )

    @validator("reward_override_efhc")
    def _v_override(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        amt = d8(v)
        if amt <= Decimal("0"):
            raise ValueError("reward_override_efhc должен быть > 0")
        return str(amt)

# -----------------------------------------------------------------------------
# CRUD заданий (без прямых денежных действий)
# -----------------------------------------------------------------------------

@router.post("/tasks", response_model=TaskOut, status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_admin)])
async def create_task_admin(payload: TaskCreate, db: AsyncSession = Depends(get_db)) -> TaskOut:
    """
    Создать задание для пользовательской витрины «Задания».
    Денег не трогает — только регистрирует сущность задания.

    Возвращает созданный объект TaskOut.
    """
    if not _TASK_SVC_AVAILABLE or admin_create_task is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="tasks_service недоступен")
    try:
        obj = await admin_create_task(db, payload.model_dump())
        return TaskOut(**obj)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("admin_create_task failed: %s", e)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Временная ошибка создания задания")

@router.get("/tasks", response_model=PaginatedTasks, dependencies=[Depends(require_admin)])
async def list_tasks_admin(
    response: Response,
    limit: conint(ge=1, le=200) = 50,
    cursor: Optional[str] = None,
    q: Optional[str] = Query(None, description="Поиск по названию"),
    active: Optional[bool] = Query(None, description="Фильтр по активности"),
    db: AsyncSession = Depends(get_db),
) -> PaginatedTasks:
    """
    Список заданий с курсорной пагинацией + ETag.
    Клиент сохраняет next_cursor и подставляет его для следующей страницы.
    """
    if not _TASK_SVC_AVAILABLE or admin_list_tasks is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="tasks_service недоступен")

    after: Optional[Dict[str, Any]] = None
    if cursor:
        try:
            after = decode_cursor(cursor)
        except Exception:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Некорректный cursor")

    try:
        data = await admin_list_tasks(db, limit=int(limit), after=after, q=q, active=active)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("admin_list_tasks failed: %s", e)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Временная ошибка списка заданий")

    items_raw: List[Dict[str, Any]] = list(data.get("items") or [])
    items = [TaskOut(**it) for it in items_raw]
    next_after = data.get("next_after")
    body = PaginatedTasks(items=items, next_cursor=(encode_cursor(next_after) if next_after else None))

    try:
        response.headers["ETag"] = etag(body.dict())
    except Exception:
        pass

    return body

@router.get("/tasks/{task_id}", response_model=TaskOut, dependencies=[Depends(require_admin)])
async def get_task_admin(task_id: int, db: AsyncSession = Depends(get_db)) -> TaskOut:
    """
    Получить одно задание по ID (read-only).
    """
    if not _TASK_SVC_AVAILABLE or admin_get_task is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="tasks_service недоступен")
    try:
        obj = await admin_get_task(db, int(task_id))
        if not obj:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Задание не найдено")
        return TaskOut(**obj)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("admin_get_task failed: %s", e)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Временная ошибка получения задания")

@router.patch("/tasks/{task_id}", response_model=TaskOut, dependencies=[Depends(require_admin)])
async def update_task_admin(task_id: int, payload: TaskUpdate, db: AsyncSession = Depends(get_db)) -> TaskOut:
    """
    Обновить поля задания (read-only деньги, без выплат).
    """
    if not _TASK_SVC_AVAILABLE or admin_update_task is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="tasks_service недоступен")
    try:
        obj = await admin_update_task(db, int(task_id), {k: v for k, v in payload.model_dump(exclude_unset=True).items()})
        if not obj:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Задание не найдено")
        return TaskOut(**obj)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("admin_update_task failed: %s", e)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Временная ошибка обновления задания")

@router.delete("/tasks/{task_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(require_admin)])
async def delete_task_admin(task_id: int, db: AsyncSession = Depends(get_db)) -> None:
    """
    Удалить задание. Денежных побочных эффектов нет.
    """
    if not _TASK_SVC_AVAILABLE or admin_delete_task is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="tasks_service недоступен")
    try:
        ok = await admin_delete_task(db, int(task_id))
        if not ok:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Задание не найдено")
        return None
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("admin_delete_task failed: %s", e)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Временная ошибка удаления задания")

# -----------------------------------------------------------------------------
# Витрина заявок по заданию (просмотр/модерация готовится на клиенте)
# -----------------------------------------------------------------------------

@router.get("/tasks/{task_id}/subs", response_model=PaginatedSubmissions, dependencies=[Depends(require_admin)])
async def list_task_submissions_admin(
    response: Response,
    task_id: int,
    limit: conint(ge=1, le=200) = 50,
    cursor: Optional[str] = None,
    status_filter: Optional[str] = Query(None, alias="status", description="PENDING/APPROVED/REJECTED"),
    db: AsyncSession = Depends(get_db),
) -> PaginatedSubmissions:
    """
    Список пользовательских заявок по конкретному заданию с курсорами + ETag.
    Используется админкой для очереди модерации.
    """
    if not _TASK_SVC_AVAILABLE or admin_list_submissions is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="tasks_service недоступен")

    after: Optional[Dict[str, Any]] = None
    if cursor:
        try:
            after = decode_cursor(cursor)
        except Exception:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Некорректный cursor")

    try:
        data = await admin_list_submissions(
            db, task_id=int(task_id), limit=int(limit), after=after, status=status_filter
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("admin_list_submissions failed: %s", e)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Временная ошибка списка заявок")

    items_raw: List[Dict[str, Any]] = list(data.get("items") or [])
    items = [SubmissionOut(**it) for it in items_raw]
    next_after = data.get("next_after")
    body = PaginatedSubmissions(items=items, next_cursor=(encode_cursor(next_after) if next_after else None))

    try:
        response.headers["ETag"] = etag(body.dict())
    except Exception:
        pass

    return body

# -----------------------------------------------------------------------------
# Модерация заявки (approve/reject) + ВЫПЛАТА бонуса (денежная операция)
# -----------------------------------------------------------------------------

@router.post(
    "/submissions/{submission_id}/moderate",
    response_model=SubmissionOut,
    dependencies=[Depends(require_admin)],
)
async def moderate_submission_admin(
    submission_id: int,
    payload: SubmissionModerateIn,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db),
) -> SubmissionOut:
    """
    Модерирует заявку пользователя:
      • approve=True  → произвести безопасную ВЫПЛАТУ бонуса через единый банк,
                        идемпотентно по Idempotency-Key.
      • approve=False → отклонить без денежных последствий.

    Требования канона:
      • ОБЯЗАТЕЛЕН заголовок Idempotency-Key для денежной операции.
      • Повтор с тем же ключом НЕ создаёт дублей выплат (read-through).
    """
    if not idempotency_key or not idempotency_key.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Idempotency-Key обязателен для денежных операций",
        )

    if not _TASK_SVC_AVAILABLE or moderate_submission_and_pay is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="tasks_service недоступен")

    try:
        sub = await moderate_submission_and_pay(
            db=db,
            submission_id=int(submission_id),
            approve=bool(payload.approve),
            moderator_tg_id=(int(payload.moderator_tg_id) if payload.moderator_tg_id is not None else None),
            reward_override_efhc=(d8(payload.reward_override_efhc) if payload.reward_override_efhc else None),
            idempotency_key=idempotency_key.strip(),
        )
        return SubmissionOut(**sub)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("moderate_submission_and_pay failed: %s", e)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Временная ошибка модерации")

# =============================================================================
# Пояснения «для чайника»:
#   • Этот файл — «админский контроллер» блока «Задания». Он отвечает за
#     правила API (Idempotency-Key/ETag/курсоры) и проверяет админ-доступ.
#     Все «деньги» исполняет tasks_service → transactions_service (банк).
#   • Если сервис недоступен (деплой, миграции) — возвращаем 503. Это нормальная
#     деградация: админка может повторить запрос позже; система не ломается.
#   • Почему нужен Idempotency-Key? Чтобы исключить двойную выплату при повторной
#     отправке запроса (refresh/повтор кнопки). Ключ должен быть уникальным на
#     стороне клиента (например, UUID). Сервер хранит его в журнале и гарантирует,
#     что вторая/третья попытка с тем же ключом не создаст дублей.
#   • Почему курсоры, а не OFFSET? Курсоры работают стабильнее на больших объёмах
#     и не «дрожат» при вставках/удалениях, плюс эффективнее по индексу (id/created_at).
# =============================================================================
