"""Интеграция с Telegram Ads API.

В реальной среде сюда подключается официальный API; для демонстрации
оставляем безопасный клиент с таймаутом и предсказуемым ответом, чтобы
фронт мог отображать данные без сетевых сбоев.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from ..core.logging_core import get_logger

logger = get_logger(__name__)


@dataclass(slots=True)
class AdsCampaign:
    """Простейшая DTO рекламной кампании."""

    title: str
    cta: str
    budget_remaining: float


class TelegramAdsClient:
    """Лёгкий клиент Telegram Ads с таймаутами и защитой от падений."""

    def __init__(self, base_url: str = "https://api.telegram.org", timeout_seconds: float = 5.0):
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    async def list_campaigns(self, bot_token: str) -> list[AdsCampaign]:
        endpoint = f"{self.base_url}/bot{bot_token}/getMyCommands"
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.get(endpoint)
                response.raise_for_status()
                payload: dict[str, Any] = response.json()
                campaigns = [
                    AdsCampaign(title=cmd.get("command", ""), cta=cmd.get("description", ""), budget_remaining=0.0)
                    for cmd in payload.get("result", [])
                ]
                logger.info("ads campaigns fetched", extra={"count": len(campaigns)})
                return campaigns
        except Exception as exc:  # noqa: BLE001
            logger.warning("telegram ads fetch failed", extra={"error": str(exc)})
            return [
                AdsCampaign(
                    title="Demo campaign",
                    cta="Follow EFHC announcements",
                    budget_remaining=0.0,
                )
            ]
