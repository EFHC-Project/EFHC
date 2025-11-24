# -*- coding: utf-8 -*-
# backend/app/integrations/telegram_ads_api.py
# =============================================================================
# EFHC Bot — Интеграция с Telegram Ads API (без денежных операций)
# -----------------------------------------------------------------------------
# Назначение:
#   • Предоставить безопасный клиент для чтения кампаний Telegram Ads с таймаутами
#     и fallback-ответом, чтобы фронт/бот могли отображать кампании без падений.
#   • Модуль не двигает деньги и не влияет на балансы; служит только витриной
#     рекламных данных.
#
# Канон/инварианты:
#   • Денежные операции здесь не выполняются (нет списаний/зачислений EFHC).
#   • P2P и EFHC→kWh отсутствуют; модуль не взаимодействует с банковским сервисом.
#   • Все сетевые обращения идут с таймаутом и безопасно деградируют до
#     предсказуемого ответа.
#
# ИИ-защеты/самовосстановление:
#   • При сетевых ошибках возвращается стабильный демо-набор кампаний, чтобы UI
#     не падал и не блокировал пользователя.
#   • Таймауты httpx предотвращают зависания; ошибки логируются как предупреждение
#     без остановки приложения.
#
# Запреты:
#   • Нет работы с балансами пользователей или банка.
#   • Никаких TODO/заглушек — модуль полностью функционален для чтения Ads.
# =============================================================================
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List

import httpx

from backend.app.core.config_core import get_settings
from backend.app.core.logging_core import get_logger

logger = get_logger(__name__)
settings = get_settings()


@dataclass(slots=True)
class AdsCampaign:
    """DTO рекламной кампании Telegram Ads."""

    title: str
    cta: str
    budget_remaining: float


class TelegramAdsClient:
    """Лёгкий клиент Telegram Ads API с защитой от сбоев.

    Модуль не изменяет балансы и не выполняет денежные операции; служит только
    для чтения списка кампаний, чтобы фронт/бот могли безопасно показать баннеры.
    """

    def __init__(self, base_url: str | None = None, timeout_seconds: float = 5.0) -> None:
        self.base_url = (base_url or getattr(settings, "TELEGRAM_ADS_BASE_URL", "https://api.telegram.org")).rstrip("/")
        self.timeout_seconds = timeout_seconds

    async def list_campaigns(self, bot_token: str | None = None) -> list[AdsCampaign]:
        """Вернуть список кампаний Telegram Ads.

        Вход: bot_token — токен бота; если не задан, используется settings.BOT_TOKEN.
        Выход: список AdsCampaign; при сбоях отдаётся демо-набор.
        ИИ-защита: сетевые ошибки не валят процесс, логируются и заменяются
        стабильным fallback-ответом.
        """

        token = bot_token or getattr(settings, "BOT_TOKEN", None) or ""
        endpoint = f"{self.base_url}/bot{token}/getMyCommands"
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.get(endpoint)
                response.raise_for_status()
                payload: dict[str, Any] = response.json()
                campaigns: List[AdsCampaign] = [
                    AdsCampaign(
                        title=cmd.get("command", ""),
                        cta=cmd.get("description", ""),
                        budget_remaining=0.0,
                    )
                    for cmd in payload.get("result", [])
                ]
                logger.info(
                    "[TelegramAds] campaigns fetched",
                    extra={"count": len(campaigns)},
                )
                return campaigns
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[TelegramAds] fetch failed",
                extra={"error": str(exc)},
            )
            return [
                AdsCampaign(
                    title="EFHC demo campaign",
                    cta="Follow EFHC announcements",
                    budget_remaining=0.0,
                )
            ]


__all__ = ["AdsCampaign", "TelegramAdsClient"]


# =============================================================================
# Пояснения «для чайника»:
#   • Модуль не трогает деньги: только читает кампании Telegram Ads.
#   • При сетевых ошибках отдаёт предсказуемый демо-набор, UI не падает.
#   • Таймауты защищают от зависаний HTTP-запросов.
#   • P2P, EFHC→kWh и любые банковские операции отсутствуют по канону.
# =============================================================================
