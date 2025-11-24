"""Команды лотерей EFHC."""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

router = Router(name="lotteries")


@router.message(Command("lotteries"))
async def handle_lotteries(message: Message) -> None:
    """Сообщить пользователю о правилах лотерей."""

    await message.answer(
        "Лотереи EFHC: билеты покупаются только за EFHC внутри бота,"
        " все операции идут через банк и Idempotency-Key."
    )
