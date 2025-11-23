"""Команды рейтинга пользователей."""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

router = Router(name="rating")


@router.message(Command("rating"))
async def handle_rating(message: Message) -> None:
    """Рассказать про рейтинг."""

    await message.answer(
        "Рейтинг обновляется фоново каждые 10 минут. Используем cursor-пагинацию"
        " и ETag для экономии трафика фронта."
    )
