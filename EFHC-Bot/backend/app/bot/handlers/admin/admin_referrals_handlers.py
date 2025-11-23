"""Админские команды по рефералам."""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

router = Router(name="admin-referrals")


@router.message(Command("admin_referrals"))
async def handle_admin_referrals(message: Message) -> None:
    """Подсказка по управлению реферальной сетью."""

    await message.answer(
        "Админ рефералов: отслеживайте активность, начисляйте бонусы через банк"
        " и уважайте Decimal(8) точность."
    )
