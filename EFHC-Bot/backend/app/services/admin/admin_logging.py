"""Сервис аудита действий админов."""

from __future__ import annotations

from ...core.logging_core import get_logger

logger = get_logger(__name__)


def log_admin_action(actor: str, action: str, payload: dict | None = None) -> None:
    """Записать действие администратора в стандартный лог."""

    logger.info("admin action", extra={"actor": actor, "action": action, "payload": payload or {}})
