"""Команды реферальной программы."""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

router = Router(name="referrals")


@router.message(Command("referrals"))
async def handle_referrals(message: Message) -> None:
    """Раскрыть базовые правила рефералок."""

    await message.answer(
        "Реферальная программа: приглашай друзей и получай бонусы в EFHC."
        " Баланс всегда хранится на сервере, фронт ничего не пересчитывает."
    )
