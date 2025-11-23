"""Служба уведомлений для админов.

Пока используем логирование; позже можно подменить на e-mail или Telegram.
"""

from __future__ import annotations

from ...core.logging_core import get_logger

logger = get_logger(__name__)


def notify_admin(subject: str, message: str) -> None:
    """Отправить уведомление администраторам (логовая реализация)."""

    logger.info("admin notification", extra={"subject": subject, "message": message})
