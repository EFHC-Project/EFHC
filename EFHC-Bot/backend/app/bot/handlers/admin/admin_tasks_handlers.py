"""Админские команды управления заданиями."""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

router = Router(name="admin-tasks")


@router.message(Command("admin_tasks"))
async def handle_admin_tasks(message: Message) -> None:
    """Подсказка по модерации заданий."""

    await message.answer(
        "Админ заданий: публикуйте активности, модерируйте доказательства и"
        " начисляйте награды через банковский сервис с Idempotency-Key."
    )
