"""Простейшие клавиатуры для быстрого доступа."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def main_menu_keyboard() -> InlineKeyboardMarkup:
    """Вернуть главное меню с ключевыми разделами."""

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Энергия", callback_data="energy")],
            [InlineKeyboardButton(text="Панели", callback_data="panels")],
            [InlineKeyboardButton(text="Shop", callback_data="shop")],
        ]
    )
