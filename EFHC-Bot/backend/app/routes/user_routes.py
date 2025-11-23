"""User-facing routes for profile management."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.deps import get_db
from ..core.utils_core import json_for_etag, stable_etag
from ..models import User

router = APIRouter()


class UserCreateRequest(BaseModel):
    telegram_id: int
    ton_wallet: str | None = None


class UserResponse(BaseModel):
    id: int
    telegram_id: int
    main_balance: str
    bonus_balance: str
    available_kwh: str
    total_generated_kwh: str
    is_vip: bool

    @classmethod
    def from_model(cls, user: User) -> "UserResponse":
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
async def register_user(payload: UserCreateRequest, db: AsyncSession = Depends(get_db)) -> UserResponse:
    existing = await db.scalar(select(User).where(User.telegram_id == payload.telegram_id))
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
