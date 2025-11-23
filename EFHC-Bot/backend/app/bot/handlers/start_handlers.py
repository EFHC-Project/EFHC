"""Стартовые команды бота EFHC."""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import Message

router = Router(name="start")


@router.message(CommandStart())
async def handle_start(message: Message) -> None:
    """Приветствие и краткая подсказка для пользователя."""

    await message.answer(
        "Привет! Я EFHC Bot. Используй /energy для энергии, /panels для панелей,"
        " /exchange для обмена kWh→EFHC и /shop для покупок."
    )
