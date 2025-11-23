"""Пользовательские команды по рекламным активностям."""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

router = Router(name="ads")


@router.message(Command("ads"))
async def handle_ads(message: Message) -> None:
    """Показать инструкции по рекламным кампаниям."""

    await message.answer(
        "Рекламные кампании доступны во вкладке Ads. Мы показываем их с ETag,"
        " чтобы экономить трафик."
    )
