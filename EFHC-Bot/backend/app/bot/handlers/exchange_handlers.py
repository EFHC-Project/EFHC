"""Команды обмена kWh → EFHC."""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

router = Router(name="exchange")


@router.message(Command("exchange"))
async def handle_exchange(message: Message) -> None:
    """Пояснение по обмену энергии на EFHC."""

    await message.answer(
        "Обмен kWh→EFHC происходит по курсу 1:1 через банк, обратной конверсии нет."
        " Все платежи требуют Idempotency-Key."
    )
