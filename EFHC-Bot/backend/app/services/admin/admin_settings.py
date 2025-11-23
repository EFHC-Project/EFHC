"""Admin settings storage."""

from __future__ import annotations

from ...core.config_core import get_core_config


class AdminSettings:
    """Чтение конфигурации для админки."""

    def __init__(self) -> None:
        self.config = get_core_config()
