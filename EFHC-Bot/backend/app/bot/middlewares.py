"""Регистрация базовых middleware для aiogram."""

from __future__ import annotations

from aiogram import BaseMiddleware
from aiogram.types import Message

from ..core.logging_core import get_logger

logger = get_logger(__name__)


class LoggingMiddleware(BaseMiddleware):
    """Логирование входящих сообщений без изменения бизнес-логики."""

    async def __call__(self, handler, event: Message, data):  # type: ignore[override]
        logger.info("bot message", extra={"from": event.from_user.id if event.from_user else None})
        return await handler(event, data)
