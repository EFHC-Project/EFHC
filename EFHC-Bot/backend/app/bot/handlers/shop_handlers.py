"""Команды магазина EFHC."""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

router = Router(name="shop")


@router.message(Command("shop"))
async def handle_shop(message: Message) -> None:
    """Показать подсказку по покупкам."""

    await message.answer(
        "Магазин EFHC: покупай пакеты EFHC и VIP NFT через TON."
        " Все оплаты логируются в ton_inbox_logs и проводятся идемпотентно."
    )
