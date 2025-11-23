"""Команды пользовательских заданий."""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

router = Router(name="tasks")


@router.message(Command("tasks"))
async def handle_tasks(message: Message) -> None:
    """Подсказка по выполнению заданий."""

    await message.answer(
        "Задания выполняются с модерацией. Награды начисляются через банк"
        " и логируются в efhc_transfers_log, повторные запросы требуют Idempotency-Key."
    )
