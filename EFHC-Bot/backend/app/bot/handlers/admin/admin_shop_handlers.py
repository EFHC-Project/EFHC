"""Админские команды витрины Shop."""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

router = Router(name="admin-shop")


@router.message(Command("admin_shop"))
async def handle_admin_shop(message: Message) -> None:
    """Подсказка по управлению магазином."""

    await message.answer(
        "Админ магазина: редактируйте пакеты EFHC и NFT. Цена 0 деактивирует карточку,"
        " оплаты ведутся через TON watcher и efhc_transfers_log."
    )
