"""Профиль пользователя: регистрация и чтение (без денежных действий)."""

from __future__ import annotations

# ======================================================================
# EFHC Bot — routes/user_routes.py
# ----------------------------------------------------------------------
# Назначение: создание пользователя и получение профиля с ETag. Денежные
#             операции здесь не выполняются.
# Канон/инварианты:
#   • Балансы не изменяются, маршруты только читают/создают пользователя.
#   • P2P и EFHC→kWh отсутствуют; Idempotency-Key не требуется, так как
#     деньги не двигаются.
#   • GET возвращает ETag и поддерживает 304.
# ИИ-защиты/самовосстановление:
#   • Повторная регистрация возвращает существующую запись (idempotent
#     read-through), не создавая дублей.
#   • ETag стабилен благодаря json_for_etag + stable_etag.
# Запреты:
#   • Нет денежных транзакций, нет OFFSET-пагинации.
# ======================================================================

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.deps import get_db
from ..core.utils_core import json_for_etag, stable_etag
from ..models import User

router = APIRouter()


class UserCreateRequest(BaseModel):
    """Запрос на регистрацию пользователя через Telegram ID.

    Вход: telegram_id (int), опциональный ton_wallet.
    Побочные эффекты: создаёт запись в таблице users, денег не двигает.
    Идемпотентность: повтор с тем же telegram_id возвращает существующего
    пользователя без дублей.
    """

    telegram_id: int
    ton_wallet: str | None = None


class UserResponse(BaseModel):
    """Ответ с публичным профилем пользователя."""

    id: int
    telegram_id: int
    main_balance: str
    bonus_balance: str
    available_kwh: str
    total_generated_kwh: str
    is_vip: bool

    @classmethod
    def from_model(cls, user: User) -> "UserResponse":
        """Построить DTO из ORM-модели без изменения балансов."""

        return cls(
            id=user.id,
            telegram_id=user.telegram_id,
            main_balance=str(user.main_balance),
            bonus_balance=str(user.bonus_balance),
            available_kwh=str(user.available_kwh),
            total_generated_kwh=str(user.total_generated_kwh),
            is_vip=user.is_vip,
        )


@router.post("/", response_model=UserResponse)
async def register_user(
    payload: UserCreateRequest, db: AsyncSession = Depends(get_db)
) -> UserResponse:
    """Создать или вернуть существующего пользователя по telegram_id.

    Вход: тело UserCreateRequest (telegram_id, ton_wallet).
    Побочные эффекты: создаёт запись users при отсутствии; не двигает деньги.
    Идемпотентность: повторный вызов с тем же telegram_id возвращает
    существующего пользователя.
    Исключения: отсутствуют, кроме ошибок БД при вставке.
    """

    existing = await db.scalar(
        select(User).where(User.telegram_id == payload.telegram_id)
    )
    if existing:
        return UserResponse.from_model(existing)
    user = User(telegram_id=payload.telegram_id, ton_wallet=payload.ton_wallet)
    db.add(user)
    await db.flush()
    return UserResponse.from_model(user)


@router.get("/{telegram_id}", response_model=UserResponse)
async def get_profile(
    telegram_id: int,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> UserResponse:
    """Вернуть профиль пользователя с ETag и 304.

    Вход: path-параметр telegram_id.
    Выход: UserResponse; при совпадении ETag отдаётся 304 без тела.
    Побочные эффекты: отсутствуют, деньги не двигаются.
    Идемпотентность: GET детерминирован, ETag стабилен.
    Исключения: 404, если пользователь не найден.
    """

    user = await db.scalar(select(User).where(User.telegram_id == telegram_id))
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    payload = UserResponse.from_model(user)
    etag = stable_etag(json_for_etag(payload.dict()))
    if request.headers.get("if-none-match") == etag:
        response.status_code = 304
        return payload
    response.headers["ETag"] = etag
    return payload


# ======================================================================
# Пояснения «для чайника»:
#   • Эти ручки не двигают EFHC и не требуют Idempotency-Key.
#   • Повторный POST с тем же telegram_id вернёт существующего пользователя.
#   • GET отдаёт ETag; If-None-Match → 304, что экономит трафик.
#   • Здесь нет P2P и EFHC→kWh — только чтение профиля и базовая регистрация.
# ======================================================================
