"""Админские команды статистики."""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

router = Router(name="admin-stats")


@router.message(Command("admin_stats"))
async def handle_admin_stats(message: Message) -> None:
    """Сообщить о доступных отчётах."""

    await message.answer(
        "Статистика: ежедневные отчёты и рейтинг формируются фоном каждые 10 минут."
        " Используйте админку для выгрузки."
    )
