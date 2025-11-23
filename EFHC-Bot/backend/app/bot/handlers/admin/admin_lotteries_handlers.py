"""Админские команды для лотерей."""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

router = Router(name="admin-lotteries")


@router.message(Command("admin_lotteries"))
async def handle_admin_lotteries(message: Message) -> None:
    """Подсказка по управлению лотереями."""

    await message.answer(
        "Админ лотерей: создавайте розыгрыши, продавайте билеты за EFHC,"
        " билеты продаются только через банк и Idempotency-Key."
    )
