"""Сервис для административных проверок."""

from __future__ import annotations

from ..core.config_core import get_core_config


class AdminService:
    """Проверка прав доступа админа по Telegram ID или API-ключу."""

    def __init__(self) -> None:
        self.config = get_core_config()

    def is_admin(self, telegram_id: int | None, api_key: str | None) -> bool:
        if telegram_id and telegram_id in self.config.admin_telegram_ids:
            return True
        if api_key and api_key == self.config.admin_api_key:
            return True
        return False
