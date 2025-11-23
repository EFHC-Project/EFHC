"""Команды вывода средств для админов."""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

router = Router(name="admin-withdrawals")


@router.message(Command("admin_withdrawals"))
async def handle_admin_withdrawals(message: Message) -> None:
    """Подсказка по обработке заявок на вывод."""

    await message.answer(
        "Админ выводов: заявки проходят вручную, все списания через банк"
        " и efhc_transfers_log с Idempotency-Key."
    )
