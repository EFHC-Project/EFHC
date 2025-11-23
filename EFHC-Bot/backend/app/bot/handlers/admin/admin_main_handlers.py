"""Админский приветственный хэндлер."""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

router = Router(name="admin-main")


@router.message(Command("admin"))
async def handle_admin_entry(message: Message) -> None:
    """Краткая справка по возможностям админов."""

    await message.answer(
        "Админ-панель: доступ по ADMIN_TELEGRAM_ID, NFT или X-Admin-Api-Key."
        " Используйте /admin_users, /admin_shop, /admin_tasks для навигации."
    )
