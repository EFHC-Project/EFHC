"""Хэндлер проверки доступности бота."""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

router = Router(name="health")


@router.message(Command("health"))
async def handle_health(message: Message) -> None:
    """Ответить пользователю, что бот работает."""

    await message.answer("EFHC Bot жив и готов к работе ✅")
