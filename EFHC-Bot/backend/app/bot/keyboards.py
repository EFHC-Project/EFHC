"""
==============================================================================
== EFHC Bot — keyboards
------------------------------------------------------------------------------
Назначение: маленькие InlineKeyboard-разметки для основных действий бота, без
бизнес-логики и без работы с балансами.

Канон/инварианты:
  • Балансы и БД не изменяет; только формирует кнопки.
  • Тексты и callback-данные должны соответствовать маршрутизации aiogram.

ИИ-защиты/самовосстановление:
  • Простая генерация клавиатур минимизирует шанс падений при локализации.
  • Отсутствие сетевых вызовов делает их безопасными при рестартах.

Запреты:
  • Не кодирует деньги/цены; не выдаёт инструкции для TON.
  • Не содержит логики VIP/NFT — только UI-слой.
==============================================================================
"""

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
