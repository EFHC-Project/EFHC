"""Команды управления пользователями для админов."""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

router = Router(name="admin-users")


@router.message(Command("admin_users"))
async def handle_admin_users(message: Message) -> None:
    """Подсказка по управлению пользователями."""

    await message.answer(
        "Админ: просматривайте пользователей, корректируйте балансы через банк"
        " и соблюдайте идемпотентность при денежных операциях."
    )
