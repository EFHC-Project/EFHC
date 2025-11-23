"""Shop endpoints for catalogue and orders."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.deps import get_db
from ..core.utils_core import cursor_from_items, json_for_etag, stable_etag
from ..models import ShopItem

router = APIRouter()


class ShopItemResponse(BaseModel):
    sku: str
    title: str
    price_efhc: str

    @classmethod
    def from_model(cls, item: ShopItem) -> "ShopItemResponse":
        return cls(sku=item.sku, title=item.title, price_efhc=str(item.price_efhc))


class ShopListResponse(BaseModel):
    items: list[ShopItemResponse]
    next_cursor: str | None
    has_more: bool


@router.get("/items", response_model=ShopListResponse)
async def list_items(
    request: Request,
    response: Response,
    cursor: int | None = None,
    limit: int = 20,
    db: AsyncSession = Depends(get_db),
) -> ShopListResponse:
    stmt = (
        select(ShopItem)
        .where(ShopItem.active.is_(True))
        .order_by(ShopItem.created_at, ShopItem.id)
        .limit(limit + 1)
    )
    if cursor:
        stmt = stmt.where(ShopItem.id > cursor)
    items = (await db.execute(stmt)).scalars().all()
    has_more = len(items) > limit
    visible_items = items[:limit]
    payload = ShopListResponse(
        items=[ShopItemResponse.from_model(item) for item in visible_items],
        next_cursor=cursor_from_items(visible_items),
        has_more=has_more,
    )
    etag = stable_etag(json_for_etag(payload.dict()))
    if request.headers.get("if-none-match") == etag:
        response.status_code = 304
        return payload
    response.headers["ETag"] = etag
    return payload
