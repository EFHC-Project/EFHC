"""Ads rotation endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.deps import get_db
from ..core.utils_core import cursor_from_items, json_for_etag, stable_etag
from ..models import AdsCampaign

router = APIRouter()


class AdsCampaignResponse(BaseModel):
    id: int
    title: str
    budget_efhc: str


class AdsListResponse(BaseModel):
    items: list[AdsCampaignResponse]
    next_cursor: str | None
    has_more: bool


@router.get("/campaigns", response_model=AdsListResponse)
async def list_campaigns(
    request: Request,
    response: Response,
    cursor: int | None = None,
    limit: int = 20,
    db: AsyncSession = Depends(get_db),
) -> AdsListResponse:
    stmt = (
        select(AdsCampaign)
        .where(AdsCampaign.active.is_(True))
        .order_by(AdsCampaign.created_at, AdsCampaign.id)
        .limit(limit + 1)
    )
    if cursor:
        stmt = stmt.where(AdsCampaign.id > cursor)
    campaigns = (await db.execute(stmt)).scalars().all()
    has_more = len(campaigns) > limit
    visible_campaigns = campaigns[:limit]
    payload = AdsListResponse(
        items=[
            AdsCampaignResponse(
                id=campaign.id,
                title=campaign.title,
                budget_efhc=str(campaign.budget_efhc),
            )
            for campaign in visible_campaigns
        ],
        next_cursor=cursor_from_items(visible_campaigns),
        has_more=has_more,
    )
    etag = stable_etag(json_for_etag(payload.dict()))
    if request.headers.get("if-none-match") == etag:
        response.status_code = 304
        return payload
    response.headers["ETag"] = etag
    return payload
