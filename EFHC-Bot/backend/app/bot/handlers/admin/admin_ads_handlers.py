"""Админские команды по рекламным кампаниям."""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

router = Router(name="admin-ads")


@router.message(Command("admin_ads"))
async def handle_admin_ads(message: Message) -> None:
    """Подсказка по управлению рекламой."""

    await message.answer(
        "Админ рекламы: управляйте витриной Ads, используем ETag и cursor-пагинацию"
        " на API."
    )
