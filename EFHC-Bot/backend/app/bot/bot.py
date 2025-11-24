"""
===============================================================================
== EFHC Bot — bot.py (aiogram entrypoint)
-------------------------------------------------------------------------------
Назначение:
  • Запуск Telegram-бота EFHC (aiogram v3) с каноническими middlewares и
    роутерами пользовательских и админских команд.
  • Настройка webhook или fallback на polling в зависимости от ENV.
  • Не содержит бизнес-логики и не двигает деньги/балансы.

Канон/инварианты:
  • Денежные операции не выполняются; балансы не изменяются (только маршрутизация
    сообщений).
  • P2P и EFHC→kWh не реализуются здесь; любая экономическая логика делегируется
    REST API/сервисам.
  • Поддерживается per-second канон генерации только как справочная информация в
    хэндлерах; никаких суточных ставок.
  • Админ-доступ проверяется на уровне REST/сервисов; бот не выдает привилегий
    сам по себе.

ИИ-защиты/самовосстановление:
  • Middleware SafeMiddleware перехватывает исключения хэндлеров, чтобы бот
    «никогда не падал» и отвечал fallback-сообщением.
  • LoggingMiddleware фиксирует базовый контекст для расследования инцидентов.
  • Fallback: если webhook не настроен, бот автоматически переключается на
    polling без остановки.

Запреты:
  • Нет прямых запросов к БД или банковскому сервису.
  • Нет денежных операций и работы с Idempotency-Key — только маршрутизация.
  • Нет автодоставки NFT/VIP, нет P2P, нет обратной конверсии EFHC→kWh.
===============================================================================
"""

from __future__ import annotations

import asyncio
from typing import Sequence

from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

from backend.app.core.config_core import get_settings
from backend.app.core.logging_core import get_logger
from .handlers import (
    ads_handlers,
    exchange_handlers,
    health_handlers,
    lotteries_handlers,
    panels_handlers,
    rating_handlers,
    referrals_handlers,
    shop_handlers,
    start_handlers,
    tasks_handlers,
)
from .handlers.admin import (
    admin_ads_handlers,
    admin_lotteries_handlers,
    admin_main_handlers,
    admin_panels_handlers,
    admin_referrals_handlers,
    admin_shop_handlers,
    admin_stats_handlers,
    admin_tasks_handlers,
    admin_users_handlers,
    admin_withdrawals_handlers,
)
from .middlewares import LoggingMiddleware, SafeMiddleware

logger = get_logger(__name__)
settings = get_settings()


# ---------------------------------------------------------------------------
# Хелперы маршрутизации
# ---------------------------------------------------------------------------


def _routers() -> Sequence:
    """Собирает все роутеры aiogram для подключения в Dispatcher.

    Назначение: единый список всех пользовательских и админских разделов,
    чтобы не пропустить домен при включении.
    """

    return (
        start_handlers.router,
        health_handlers.router,
        panels_handlers.router,
        exchange_handlers.router,
        shop_handlers.router,
        tasks_handlers.router,
        referrals_handlers.router,
        rating_handlers.router,
        lotteries_handlers.router,
        ads_handlers.router,
        admin_main_handlers.router,
        admin_users_handlers.router,
        admin_panels_handlers.router,
        admin_shop_handlers.router,
        admin_tasks_handlers.router,
        admin_lotteries_handlers.router,
        admin_referrals_handlers.router,
        admin_withdrawals_handlers.router,
        admin_stats_handlers.router,
        admin_ads_handlers.router,
    )


def _setup_middlewares(dp: Dispatcher) -> None:
    """Добавляет canon middlewares (логирование, защита от падений).

    Побочные эффекты: модифицирует Dispatcher (middlewares stack), но не
    взаимодействует с БД или балансами.
    """

    dp.update.middleware(LoggingMiddleware())
    dp.update.middleware(SafeMiddleware())


def _build_dispatcher() -> Dispatcher:
    """Создаёт Dispatcher с памятью FSM и подключает роутеры/мидлвары.

    Идемпотентность: повторное создание вернёт новый экземпляр, но каждый
    экземпляр содержит один и тот же набор роутеров/мидлваров.
    """

    dp = Dispatcher(storage=MemoryStorage())
    for router in _routers():
        dp.include_router(router)
    _setup_middlewares(dp)
    return dp


def _bot_commands() -> Sequence[BotCommand]:
    """Базовый набор команд, чтобы пользователям было проще навигировать."""

    return (
        BotCommand(command="start", description="Запуск и помощь"),
        BotCommand(command="energy", description="Баланс энергии"),
        BotCommand(command="panels", description="Панели и генерация"),
        BotCommand(command="exchange", description="Обмен kWh→EFHC"),
        BotCommand(command="shop", description="Магазин EFHC/VIP"),
        BotCommand(command="tasks", description="Задания"),
        BotCommand(command="ads", description="Реклама"),
    )


async def _configure_commands(bot: Bot) -> None:
    """Устанавливает список команд в Telegram. Без побочных эффектов на деньги."""

    await bot.set_my_commands(list(_bot_commands()))


async def _start_polling(bot: Bot, dp: Dispatcher) -> None:
    """Запуск бота в режиме polling (fallback, если webhook недоступен).

    ИИ-защита: удаляем вебхук перед polling, чтобы исключить двойную доставку.
    """

    await bot.delete_webhook(drop_pending_updates=True)
    await _configure_commands(bot)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


async def _start_webhook(bot: Bot, dp: Dispatcher) -> None:
    """Запускает aiohttp веб-сервер для приёма вебхуков Telegram.

    Если WEBHOOK_BASE_URL не задан или выключен флаг, переключается на polling.
    """

    webhook_url = settings.build_tg_webhook_url()
    if not webhook_url:
        logger.warning("Webhook not configured; falling back to polling")
        await _start_polling(bot, dp)
        return

    secret = settings.webhook_secret_effective
    await bot.set_webhook(
        url=webhook_url,
        secret_token=secret,
        drop_pending_updates=True,
    )
    await _configure_commands(bot)

    app = web.Application()
    handler = SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=secret)
    handler.register(app, path=settings.TELEGRAM_WEBHOOK_PATH)

    legacy_path = settings.TELEGRAM_WEBHOOK_PATH_LEGACY
    if legacy_path and legacy_path != settings.TELEGRAM_WEBHOOK_PATH:
        handler.register(app, path=legacy_path)

    setup_application(app, dp, bot=bot)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(
        runner,
        host=settings.APP_HOST or "0.0.0.0",
        port=settings.APP_PORT or 8000,
    )
    logger.info(
        "Starting webhook bot",
        extra={"url": webhook_url, "host": settings.APP_HOST, "port": settings.APP_PORT},
    )
    await site.start()
    # Блокируемся пока не будет прервано (ctrl+c или сигнал)
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await runner.cleanup()


async def _run_async() -> None:
    """Создаёт бота, настраивает диспетчер и запускает webhook/polling."""

    if not settings.TELEGRAM_BOT_TOKEN:
        logger.error("BOT_TOKEN is not set; bot will not start")
        return

    bot = Bot(token=settings.TELEGRAM_BOT_TOKEN, parse_mode=ParseMode.HTML)
    dp = _build_dispatcher()

    if settings.WEBHOOK_ENABLED:
        await _start_webhook(bot, dp)
    else:
        await _start_polling(bot, dp)


def run_bot() -> None:
    """Синхронный entrypoint для запуска из CLI/процесса."""

    try:
        asyncio.run(_run_async())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")


if __name__ == "__main__":
    run_bot()

# ===========================================================================
# Пояснения «для чайника»:
#   • Этот файл настраивает aiogram-бот: токен, middlewares, роутеры, webhook/
#     polling. Денег не двигает, в БД не пишет.
#   • SafeMiddleware ловит ошибки хэндлеров, чтобы бот не падал; в ответ
#     отправляется мягкое сообщение о восстановлении.
#   • Если вебхук не настроен, бот сам перейдёт на polling и продолжит работу.
#   • Команды /start, /energy, /panels, /exchange, /shop, /tasks, /ads — только
#     навигация; реальные денежные операции выполняются REST API через банк.
# ===========================================================================
