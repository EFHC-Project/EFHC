"""Админские команды управления панелями."""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

router = Router(name="admin-panels")


@router.message(Command("admin_panels"))
async def handle_admin_panels(message: Message) -> None:
    """Подсказка по админ-операциям с панелями."""

    await message.answer(
        "Админ панелей: создавайте и архивируйте панели, лимит активных 1000,"
        " жизнь 180 дней. Все покупки идут через банк."
    )
