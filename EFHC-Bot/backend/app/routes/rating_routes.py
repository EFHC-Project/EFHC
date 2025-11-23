"""Rating endpoints using snapshot table."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.deps import get_db
from ..core.utils_core import cursor_from_items, json_for_etag, stable_etag
from ..models import RatingSnapshot

router = APIRouter()


class RatingEntry(BaseModel):
    user_id: int
    score: int


class RatingListResponse(BaseModel):
    items: list[RatingEntry]
    next_cursor: str | None
    has_more: bool


@router.get("/", response_model=RatingListResponse)
async def list_rating(
    request: Request,
    response: Response,
    cursor: int | None = None,
    limit: int = 20,
    db: AsyncSession = Depends(get_db),
) -> RatingListResponse:
    stmt = select(RatingSnapshot).order_by(RatingSnapshot.id).limit(limit + 1)
    if cursor:
        stmt = stmt.where(RatingSnapshot.id > cursor)
    snapshots = (await db.execute(stmt)).scalars().all()
    has_more = len(snapshots) > limit
    visible_items = snapshots[:limit]
    next_cursor = cursor_from_items(visible_items)
    items = [RatingEntry(user_id=item.user_id, score=item.score) for item in visible_items]
    payload = RatingListResponse(items=items, next_cursor=next_cursor, has_more=has_more)
    etag = stable_etag(json_for_etag(payload.dict()))
    if request.headers.get("if-none-match") == etag:
        response.status_code = 304
        return payload
    response.headers["ETag"] = etag
    return payload
