"""Команды для работы с солнечными панелями."""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

router = Router(name="panels")


@router.message(Command("panels"))
async def handle_panels(message: Message) -> None:
    """Показать справку по панелям."""

    await message.answer(
        "Раздел Панели: здесь покупаем панели за 100 EFHC, срок жизни 180 дней."
        " Проверяй остаток энергии через /energy."
    )
