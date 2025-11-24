# -*- coding: utf-8 -*-
# backend/app/core/config_core.py
# =============================================================================
# Назначение:
#   • Единый конфигурационный модуль EFHC Bot (FastAPI + SQLAlchemy async).
#   • Канонический источник всех настроек (экономика, Банк, планировщик, TON).
#
# Канон / инварианты EFHC:
#   1) Единственный курс: 1 EFHC = 1 kWh. Обратной конвертации НЕТ.
#      Любая попытка изменить курс через ENV будет проигнорирована.
#   2) Генерация задаётся ТОЛЬКО посекундно:
#        GEN_PER_SEC_BASE_KWH
#        GEN_PER_SEC_VIP_KWH
#      Никаких суточных/почасовых ставок.
#   3) Пользователям запрещено уходить в минус по любому балансу.
#      Банку EFHC разрешён минус (операции не блокируются).
#   4) P2P-переводы между пользователями запрещены (жёстко).
#      Все денежные операции только через Банк EFHC.
#   5) Любые денежные POST требуют Idempotency-Key (строго).
#   6) Начальный баланс Банка EFHC: 5 000 000 EFHC (канон проекта).
#
# ИИ-защита / самодиагностика:
#   • configure_decimal_context() настраивает Decimal (ROUND_DOWN + достаточный
#     precision) — единый стиль расчётов.
#   • initialize_runtime() проверяет DSN, создаёт локальные артефакты, выводит
#     предупреждения по секретам и TON-адресу.
#   • Валидаторы Pydantic жёстко фиксируют EFHC_KWH_RATE=1.0 и запрещают
#     отключать FORBID_P2P_TRANSFERS.
#
# Стандарты:
#   • PEP 8, длина строки ≤ 88 символов.
#   • Black-подобное форматирование.
#   • flake8/ruff-подобный линтинг (нет лишних импортов, голых except и т.п.).
#   • mypy-подобная типизация (везде, где возможно, явные типы).
# =============================================================================

from __future__ import annotations

import re
from decimal import Decimal, ROUND_DOWN, getcontext
from functools import lru_cache
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from pydantic import BaseSettings, Field, validator


# =============================================================================
# Вспомогательные утилиты (локальные, без сетевых вызовов)
# =============================================================================


def _parse_csv(value: object) -> List[str]:
    """Преобразует CSV-строку 'a,b,c' в ['a', 'b', 'c'] (пробелы обрезаются)."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(x).strip() for x in value if str(x).strip()]
    s = str(value).strip()
    if not s:
        return []
    return [item.strip() for item in s.split(",") if item.strip()]


def _unique(items: Iterable[str]) -> List[str]:
    """Возвращает элементы без повторов, сохраняя порядок первого появления."""
    out: List[str] = []
    for item in items:
        if item not in out:
            out.append(item)
    return out


def _is_probably_ton_address(addr: str) -> bool:
    """
    Быстрая проверка TON-адреса в user-friendly/base64url виде:
      • начинается с 'EQ' или 'UQ';
      • содержит только base64url-символы;
      • длина ≥ 48 символов.
    Это не строгая проверка, но хорошо отсекает мусор.
    """
    if not addr or len(addr) < 48:
        return False
    if not (addr.startswith("EQ") or addr.startswith("UQ")):
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9_\-]+", addr))


def _quantize_str(value: float | Decimal, places: int = 8) -> str:
    """Строковое представление числа с нужной точностью (ROUND_DOWN)."""
    q = Decimal(10) ** -places
    d = Decimal(str(value)).quantize(q, rounding=ROUND_DOWN)
    return f"{d:.{places}f}"


# =============================================================================
# Док-описания полей (используются в Swagger и как подсказки «для чайника»)
# =============================================================================


class _Doc:
    # Приложение
    PROJECT_NAME = "Имя проекта (отображается в Swagger/health)."
    ENV = "Окружение: production/dev/local (нормализуется в prod/dev/local)."
    DEBUG = "Расширенные логи и трассировки (только для dev/local)."
    APP_VERSION = "Версия приложения (попадает в /health)."

    APP_HOST = "Адрес для uvicorn (обычно 0.0.0.0)."
    APP_PORT = "Порт для uvicorn (например, 8000)."
    APP_RELOAD = "Горячая перезагрузка (для разработки)."
    API_PREFIX = "Префикс REST API, например /api."
    DOCS_URL = "Путь Swagger UI (/docs)."
    REDOC_URL = "Путь Redoc (/redoc)."
    OPENAPI_URL = "Путь OpenAPI JSON (/openapi.json)."

    # БД
    DATABASE_URL = (
        "DSN PostgreSQL/Neon. "
        "Будет автоматически приведён к async (postgresql+asyncpg://)."
    )
    DB_POOL_SIZE = "Размер пула соединений SQLAlchemy."
    DB_MAX_OVERFLOW = "Дополнительные соединения в пике."
    DB_SCHEMA_CORE = "Схема с ядром (users, panels, balances и т.п.)."
    DB_SCHEMA_ADMIN = "Схема админки/банковских сущностей."
    DB_SCHEMA_REFERRAL = "Схема реферальной системы."
    DB_SCHEMA_LOTTERY = "Схема лотерей."
    DB_SCHEMA_TASKS = "Схема заданий."

    # Telegram
    TELEGRAM_BOT_TOKEN = "Токен бота (env: BOT_TOKEN)."
    ADMIN_TELEGRAM_ID = "Главный админ (получает алерты)."
    BANK_TELEGRAM_ID = "Телеграм-ID счёта Банка EFHC."
    WEBHOOK_ENABLED = "Включить webhook (иначе polling)."
    WEBHOOK_BASE_URL = "Публичный базовый URL вебхука."
    WEBHOOK_SECRET = "Секрет заголовка x-telegram-bot-api-secret-token."
    TELEGRAM_WEBHOOK_PATH = "Путь вебхука (например, /tg/webhook)."
    TELEGRAM_WEBHOOK_PATH_LEGACY = "Легаси-путь для обратной совместимости."

    # TON
    TON_WALLET_ADDRESS = "TON-кошелёк проекта, на который приходят платежи."
    TON_API_KEY = "API-ключ TonAPI."
    TON_API_URL = "Базовый URL TonAPI."
    TON_WATCHER_ENABLED = "Включить фонового вотчера входящих транзакций."
    TON_WATCHER_POLL_INTERVAL = "Интервал опроса TonAPI (сек)."
    TON_MEMO_PREFIX_EFHC = "Префикс MEMO для простого депозита EFHC<tgid>."
    TON_MEMO_SKU_PREFIX = "Префикс SKU для MEMO покупок из Shop (SKU:...)."

    # Jettons / NFT
    USDT_JETTON_MASTER = "Master-адрес USDT jetton."
    EFHC_JETTON_MASTER = "Master-адрес EFHC jetton (если используется)."
    VIP_NFT_COLLECTION = "Коллекция NFT, дающая VIP-ставку."
    ADMIN_NFT_WHITELIST = "TON-адреса, которым разрешён админ-доступ."

    # Экономика
    EFHC_KWH_RATE = "Курс kWh→EFHC (жёстко фиксирован на 1.0)."
    GEN_PER_SEC_BASE_KWH = "Базовая ставка генерации kWh/сек."
    GEN_PER_SEC_VIP_KWH = "VIP-ставка генерации kWh/сек."
    PANEL_PRICE_EFHC = "Цена одной панели (EFHC)."
    PANEL_LIFESPAN_DAYS = "Срок жизни панели (дней)."
    MAX_ACTIVE_PANELS_PER_USER = "Максимум активных панелей у пользователя."
    BANK_INITIAL_BALANCE_EFHC = "Начальный баланс Банка EFHC (канон)."

    # Инварианты/безопасность
    FORBID_NEGATIVE_USER_BALANCE = "Запрет на отрицательные балансы у пользователей."
    ALLOW_NEGATIVE_BANK_BALANCE = "Разрешить отрицательный баланс Банка EFHC."
    REQUIRE_IDEMPOTENCY_HEADER = "Требовать Idempotency-Key для денежных POST."
    CANON_GEN_PER_SEC_ONLY = (
        "Генерация только через посекундные ставки (единственный источник истины)."
    )
    FORBID_P2P_TRANSFERS = (
        "Запрет P2P-переводов между пользователями (жёсткий канон)."
    )

    # Рефералка
    REFERRAL_DIRECT_BONUS_EFHC = "Моментальный бонус пригласителю (EFHC)."
    REF_BONUS_ON_ACTIVATION_EFHC = "Бонус при первой панели реферала (EFHC)."
    REF_BONUS_THRESHOLDS = "Пороги рефералки: '10:1,100:10,1000:100,...'."

    # Вывод
    WITHDRAWALS_ENABLED = "Разрешить заявки на вывод EFHC."
    WITHDRAW_MIN_EFHC = "Минимальная сумма на вывод (EFHC)."
    WITHDRAW_FEE_EFHC = "Комиссия на вывод (EFHC)."

    # Точности/округление
    EFHC_DECIMALS = "Количество знаков EFHC (обычно 8)."
    KWH_DECIMALS = "Количество знаков kWh (обычно 8)."
    KWH_DISPLAY_DECIMALS = "Точность показа kWh во фронте."
    ROUNDING_MODE = "Режим округления Decimal (по канону — DOWN)."

    # Веб/локализация
    CORS_ORIGINS = "Список разрешённых Origin (CSV)."
    SUPPORTED_LANGS = "Поддерживаемые языки (CSV)."
    DEFAULT_LANG = "Язык по умолчанию."

    # Лотереи/задания
    LOTTERY_TICKET_PRICE_EFHC = "Цена билета (EFHC)."
    LOTTERY_MAX_TICKETS_PER_USER = "Максимум билетов у пользователя."
    TASKS_ENABLED = "Включить задания."
    TASK_REWARD_BONUS_EFHC_DEFAULT = "Награда за задание (бонусные EFHC)."
    TASK_PRICE_USD_DEFAULT = "Ориентировочная цена задания в USD."

    # Планировщик
    SCHEDULER_TICK_SECONDS = (
        "Единый тик планировщика (сек). Рекомендуется 600 (10 минут)."
    )
    NETWORK_REQUEST_TIMEOUT_SEC = "Таймаут сетевых запросов (сек)."
    MAX_PARALLEL_SCHEDULER_TASKS = "Лимит параллельных задач планировщика."
    TASK_TIMEOUT_SECONDS = "Таймаут одной задачи (сек)."

    # Security / Idempotency / rate-limit
    HMAC_SECRET = "HMAC-секрет для внутренних подписей (опционально)."
    IDEMPOTENCY_TTL_SEC = "TTL для ключей идемпотентности (сек)."
    RATE_LIMIT_WINDOW_SEC = "Окно лимитирования запросов (сек)."
    RATE_LIMIT_MAX_REQ = "Максимум запросов в окне (шт)."

    # Logging
    LOG_LEVEL = "Уровень логирования (INFO/DEBUG/WARNING/ERROR)."
    LOG_JSON = "Лог в JSON (true/false)."
    LOG_COLOR = "Цветной лог для разработки (true/false)."


# =============================================================================
# Настройки приложения (единственный источник истины)
# =============================================================================


class Settings(BaseSettings):
    """
    Контейнер переменных окружения EFHC Bot.

    Важное:
      • Секреты берём только из ENV — в код не шьём.
      • Никаких суточных расписаний — все фоновые задачи живут в тике.
      • Decimal настроен на ROUND_DOWN и достаточный precision.
      • Канон EFHC (курс, генерация, P2P-запрет, начальный баланс банка)
        закреплён здесь и проверяется валидаторами.
    """

    # --------------------------- БАЗОВЫЕ НАСТРОЙКИ ---------------------------
    PROJECT_NAME: str = Field("EFHC Bot", description=_Doc.PROJECT_NAME)
    ENV: str = Field("production", description=_Doc.ENV)
    DEBUG: bool = Field(False, description=_Doc.DEBUG)
    APP_VERSION: str = Field("1.0.0", description=_Doc.APP_VERSION)

    APP_HOST: str = Field("0.0.0.0", description=_Doc.APP_HOST)
    APP_PORT: int = Field(8000, description=_Doc.APP_PORT)
    APP_RELOAD: bool = Field(True, description=_Doc.APP_RELOAD)

    API_PREFIX: str = Field("/api", description=_Doc.API_PREFIX)
    DOCS_URL: str = Field("/docs", description=_Doc.DOCS_URL)
    REDOC_URL: str = Field("/redoc", description=_Doc.REDOC_URL)
    OPENAPI_URL: str = Field("/openapi.json", description=_Doc.OPENAPI_URL)

    # --------------------------------- БАЗА ----------------------------------
    DATABASE_URL: Optional[str] = Field(None, description=_Doc.DATABASE_URL)
    DB_POOL_SIZE: int = Field(10, description=_Doc.DB_POOL_SIZE)
    DB_MAX_OVERFLOW: int = Field(10, description=_Doc.DB_MAX_OVERFLOW)

    DB_SCHEMA_CORE: str = Field("efhc_core", description=_Doc.DB_SCHEMA_CORE)
    DB_SCHEMA_ADMIN: str = Field("efhc_admin", description=_Doc.DB_SCHEMA_ADMIN)
    DB_SCHEMA_REFERRAL: str = Field(
        "efhc_referrals",
        description=_Doc.DB_SCHEMA_REFERRAL,
    )
    DB_SCHEMA_LOTTERY: str = Field(
        "efhc_lottery",
        description=_Doc.DB_SCHEMA_LOTTERY,
    )
    DB_SCHEMA_TASKS: str = Field("efhc_tasks", description=_Doc.DB_SCHEMA_TASKS)

    # ------------------------------- TELEGRAM --------------------------------
    TELEGRAM_BOT_TOKEN: Optional[str] = Field(
        None,
        env="BOT_TOKEN",
        description=_Doc.TELEGRAM_BOT_TOKEN,
    )
    ADMIN_TELEGRAM_ID: int = Field(
        362746228,
        description=_Doc.ADMIN_TELEGRAM_ID,
    )
    BANK_TELEGRAM_ID: int = Field(
        362746228,
        description=_Doc.BANK_TELEGRAM_ID,
    )

    WEBHOOK_ENABLED: bool = Field(True, description=_Doc.WEBHOOK_ENABLED)
    WEBHOOK_BASE_URL: Optional[str] = Field(
        None,
        description=_Doc.WEBHOOK_BASE_URL,
    )
    WEBHOOK_SECRET: Optional[str] = Field(
        None,
        description=_Doc.WEBHOOK_SECRET,
    )

    TELEGRAM_WEBHOOK_PATH: str = Field(
        "/tg/webhook",
        description=_Doc.TELEGRAM_WEBHOOK_PATH,
    )
    TELEGRAM_WEBHOOK_PATH_LEGACY: str = Field(
        "/telegram/webhook",
        description=_Doc.TELEGRAM_WEBHOOK_PATH_LEGACY,
    )

    # ---------------------------------- TON ----------------------------------
    TON_WALLET_ADDRESS: str = Field(
        "UQAyCoxmxzb2D6cmlf4M8zWYFYkaQuHbN_dgH-IfraFP8QKW",
        description=_Doc.TON_WALLET_ADDRESS,
    )
    TON_API_KEY: Optional[str] = Field(
        None,
        description=_Doc.TON_API_KEY,
    )
    TON_API_URL: str = Field(
        "https://tonapi.io",
        description=_Doc.TON_API_URL,
    )

    TON_WATCHER_ENABLED: bool = Field(
        True,
        description=_Doc.TON_WATCHER_ENABLED,
    )
    TON_WATCHER_POLL_INTERVAL: int = Field(
        600,
        description=_Doc.TON_WATCHER_POLL_INTERVAL,
    )

    TON_MEMO_PREFIX_EFHC: str = Field(
        "EFHC",
        description=_Doc.TON_MEMO_PREFIX_EFHC,
    )
    TON_MEMO_SKU_PREFIX: str = Field(
        "SKU",
        description=_Doc.TON_MEMO_SKU_PREFIX,
    )

    # Jettons (если нужно на уровне интеграций)
    USDT_JETTON_MASTER: str = Field(
        "EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs",
        description=_Doc.USDT_JETTON_MASTER,
    )
    EFHC_JETTON_MASTER: str = Field(
        "EQDNcpWf9mXgSPDubDCzvuaX-juL4p8MuUwrQC-36sARRBuw",
        description=_Doc.EFHC_JETTON_MASTER,
    )

    # --------------------------------- NFT/VIP -------------------------------
    VIP_NFT_COLLECTION: str = Field(
        "EQASPXkEI0NsZQzqkPjk6O_i752LfwSWRFT9WzDc2SJ2zgi0",
        description=_Doc.VIP_NFT_COLLECTION,
    )
    ADMIN_NFT_WHITELIST: List[str] = Field(
        default_factory=lambda: [
            "EQDvvZCMEs5WIOrdO4r-F9NmsyEU5HyVN0uo1yfAqLG3qyLj"
        ],
        description=_Doc.ADMIN_NFT_WHITELIST,
    )

    @validator("ADMIN_NFT_WHITELIST", pre=True)
    def _v_admin_nft_whitelist(cls, value: object) -> List[str]:
        return _parse_csv(value)

    # -------------------------------- ЭКОНОМИКА ------------------------------
    EFHC_KWH_RATE: float = Field(
        1.0,
        description=_Doc.EFHC_KWH_RATE,
    )

    # Канон: ТОЛЬКО per-second ставки
    GEN_PER_SEC_BASE_KWH: float = Field(
        0.00000692,
        description=_Doc.GEN_PER_SEC_BASE_KWH,
    )
    GEN_PER_SEC_VIP_KWH: float = Field(
        0.00000741,
        description=_Doc.GEN_PER_SEC_VIP_KWH,
    )

    PANEL_PRICE_EFHC: float = Field(
        100.0,
        description=_Doc.PANEL_PRICE_EFHC,
    )
    PANEL_LIFESPAN_DAYS: int = Field(
        180,
        description=_Doc.PANEL_LIFESPAN_DAYS,
    )
    MAX_ACTIVE_PANELS_PER_USER: int = Field(
        1000,
        description=_Doc.MAX_ACTIVE_PANELS_PER_USER,
    )

    # Начальный баланс Банка EFHC (используется миграциями/банком)
    BANK_INITIAL_BALANCE_EFHC: float = Field(
        5_000_000.0,
        description=_Doc.BANK_INITIAL_BALANCE_EFHC,
    )

    # ------------------------------ ИНВАРИАНТЫ/SEC ---------------------------
    FORBID_NEGATIVE_USER_BALANCE: bool = Field(
        True,
        description=_Doc.FORBID_NEGATIVE_USER_BALANCE,
    )
    ALLOW_NEGATIVE_BANK_BALANCE: bool = Field(
        True,
        description=_Doc.ALLOW_NEGATIVE_BANK_BALANCE,
    )
    REQUIRE_IDEMPOTENCY_HEADER: bool = Field(
        True,
        description=_Doc.REQUIRE_IDEMPOTENCY_HEADER,
    )
    CANON_GEN_PER_SEC_ONLY: bool = Field(
        True,
        description=_Doc.CANON_GEN_PER_SEC_ONLY,
    )
    FORBID_P2P_TRANSFERS: bool = Field(
        True,
        description=_Doc.FORBID_P2P_TRANSFERS,
    )

    # -------------------------------- РЕФЕРАЛКА ------------------------------
    REFERRAL_DIRECT_BONUS_EFHC: float = Field(
        0.1,
        description=_Doc.REFERRAL_DIRECT_BONUS_EFHC,
    )
    REF_BONUS_ON_ACTIVATION_EFHC: float = Field(
        0.10000000,
        description=_Doc.REF_BONUS_ON_ACTIVATION_EFHC,
    )
    REF_BONUS_THRESHOLDS: str = Field(
        "10:1,100:10,1000:100,3000:300,10000:1000",
        description=_Doc.REF_BONUS_THRESHOLDS,
    )

    # ---------------------------------- ВЫВОД --------------------------------
    WITHDRAWALS_ENABLED: bool = Field(
        True,
        description=_Doc.WITHDRAWALS_ENABLED,
    )
    WITHDRAW_MIN_EFHC: float = Field(
        10.0,
        description=_Doc.WITHDRAW_MIN_EFHC,
    )
    WITHDRAW_FEE_EFHC: float = Field(
        0.0,
        description=_Doc.WITHDRAW_FEE_EFHC,
    )

    # ------------------------------- ТОЧНОСТИ --------------------------------
    EFHC_DECIMALS: int = Field(
        8,
        description=_Doc.EFHC_DECIMALS,
    )
    KWH_DECIMALS: int = Field(
        8,
        description=_Doc.KWH_DECIMALS,
    )
    KWH_DISPLAY_DECIMALS: int = Field(
        8,
        description=_Doc.KWH_DISPLAY_DECIMALS,
    )
    ROUNDING_MODE: str = Field(
        "DOWN",
        description=_Doc.ROUNDING_MODE,
    )

    # ---------------------------- ВЕБ/ЛОКАЛИЗАЦИЯ ----------------------------
    CORS_ORIGINS: List[str] = Field(
        default_factory=lambda: [
            "http://localhost:3000",
            "https://efhc-web.vercel.app",
            "https://efhc.vercel.app",
        ],
        description=_Doc.CORS_ORIGINS,
    )

    @validator("CORS_ORIGINS", pre=True)
    def _v_cors_origins(cls, value: object) -> List[str]:
        return _unique(_parse_csv(value))

    SUPPORTED_LANGS: List[str] = Field(
        default_factory=lambda: [
            "ru",
            "en",
            "ua",
            "de",
            "fr",
            "es",
            "it",
            "pl",
        ],
        description=_Doc.SUPPORTED_LANGS,
    )
    DEFAULT_LANG: str = Field(
        "ru",
        description=_Doc.DEFAULT_LANG,
    )

    @validator("SUPPORTED_LANGS", pre=True)
    def _v_supported_langs(cls, value: object) -> List[str]:
        parsed = _parse_csv(value)
        if not parsed:
            return ["ru", "en", "ua", "de", "fr", "es", "it", "pl"]
        return parsed

    # ------------------------------ ЛОТЕРЕИ/TASKS ----------------------------
    LOTTERY_TICKET_PRICE_EFHC: float = Field(
        1.00000000,
        description=_Doc.LOTTERY_TICKET_PRICE_EFHC,
    )
    LOTTERY_MAX_TICKETS_PER_USER: int = Field(
        10,
        description=_Doc.LOTTERY_MAX_TICKETS_PER_USER,
    )

    TASKS_ENABLED: bool = Field(
        True,
        description=_Doc.TASKS_ENABLED,
    )
    TASK_REWARD_BONUS_EFHC_DEFAULT: float = Field(
        1.0,
        description=_Doc.TASK_REWARD_BONUS_EFHC_DEFAULT,
    )
    TASK_PRICE_USD_DEFAULT: float = Field(
        0.3,
        description=_Doc.TASK_PRICE_USD_DEFAULT,
    )

    # ------------------------------- ПЛАНИРОВЩИК -----------------------------
    SCHEDULER_TICK_SECONDS: int = Field(
        600,
        description=_Doc.SCHEDULER_TICK_SECONDS,
    )
    NETWORK_REQUEST_TIMEOUT_SEC: int = Field(
        20,
        description=_Doc.NETWORK_REQUEST_TIMEOUT_SEC,
    )
    MAX_PARALLEL_SCHEDULER_TASKS: int = Field(
        3,
        description=_Doc.MAX_PARALLEL_SCHEDULER_TASKS,
    )
    TASK_TIMEOUT_SECONDS: int = Field(
        900,
        description=_Doc.TASK_TIMEOUT_SECONDS,
    )

    # --------------------------------- SECURITY ------------------------------
    HMAC_SECRET: Optional[str] = Field(
        None,
        description=_Doc.HMAC_SECRET,
    )
    IDEMPOTENCY_TTL_SEC: int = Field(
        600,
        description=_Doc.IDEMPOTENCY_TTL_SEC,
    )
    RATE_LIMIT_WINDOW_SEC: int = Field(
        60,
        description=_Doc.RATE_LIMIT_WINDOW_SEC,
    )
    RATE_LIMIT_MAX_REQ: int = Field(
        300,
        description=_Doc.RATE_LIMIT_MAX_REQ,
    )

    # --------------------------- Pydantic BaseSettings -----------------------
    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = True

    # =========================== ВАЛИДАТОРЫ (ИИ-защита) ======================

    @validator("EFHC_KWH_RATE", pre=True, always=True)
    def _v_fix_efhc_rate(cls, value: object) -> float:
        """
        Канон: курс 1 EFHC = 1 kWh.
        Любое значение из ENV будет проигнорировано, но мы напечатаем предупреждение.
        """
        try:
            raw = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            raw = 1.0
        if raw != 1.0:
            print(
                "[WARN] EFHC_KWH_RATE переопределён в ENV, "
                "но канон фиксирует курс 1.0. Применяем 1.0.",
            )
        return 1.0

    @validator("GEN_PER_SEC_BASE_KWH", "GEN_PER_SEC_VIP_KWH", pre=True)
    def _v_gen_rates(cls, value: object) -> float:
        """Генерация должна быть > 0 и достаточно маленькой (защита от опечаток)."""
        try:
            val = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            raise ValueError("GEN_PER_SEC_*_KWH должны быть числом > 0") from None
        if val <= 0:
            raise ValueError("GEN_PER_SEC_*_KWH должны быть > 0")
        if val >= 1:
            raise ValueError(
                "GEN_PER_SEC_*_KWH >= 1 выглядит как ошибка (слишком большая ставка)",
            )
        return val

    @validator("FORBID_P2P_TRANSFERS", pre=True, always=True)
    def _v_forbid_p2p(cls, value: object) -> bool:
        """
        Канон запрещает P2P-переводы. Даже если в ENV поставили false,
        мы принудительно включаем запрет и выводим предупреждение.
        """
        text_value = str(value).strip().lower()
        if text_value in {"false", "0", "no"}:
            print(
                "[WARN] Попытка отключить FORBID_P2P_TRANSFERS "
                "нарушает канон EFHC. Применяем True.",
            )
        return True

    @validator("TON_WALLET_ADDRESS")
    def _v_ton_wallet(cls, value: str) -> str:
        """Мягкая проверка TON-адреса (ИИ-подсказка, но не жёсткий запрет)."""
        if not _is_probably_ton_address(value):
            print(
                "[WARN] TON_WALLET_ADDRESS выглядит нетипично. "
                "Проверьте корректность адреса.",
            )
        return value

    # =========================== Удобные свойства/методы =====================

    # ---- ENV флаги ----
    @property
    def env_normalized(self) -> str:
        """Нормализует ENV к одному из: prod/dev/local."""
        value = (self.ENV or "").strip().lower()
        if value.startswith("prod") or value == "production":
            return "prod"
        if value.startswith("dev"):
            return "dev"
        if value.startswith("loc") or value == "local":
            return "local"
        return "prod"

    @property
    def is_prod(self) -> bool:
        return self.env_normalized == "prod"

    @property
    def is_dev(self) -> bool:
        return self.env_normalized == "dev"

    @property
    def is_local(self) -> bool:
        return self.env_normalized == "local"

    # ---- Telegram webhook ----
    @property
    def webhook_secret_effective(self) -> Optional[str]:
        """Секрет вебхука активен только если он задан и webhook включён."""
        if not self.WEBHOOK_ENABLED:
            return None
        return self.WEBHOOK_SECRET

    def build_tg_webhook_url(self) -> Optional[str]:
        """Формирует URL вебхука: <WEBHOOK_BASE_URL><TELEGRAM_WEBHOOK_PATH>."""
        if not self.WEBHOOK_ENABLED or not self.WEBHOOK_BASE_URL:
            return None
        base = self.WEBHOOK_BASE_URL.rstrip("/")
        path = (self.TELEGRAM_WEBHOOK_PATH or "/tg/webhook").strip()
        if not path.startswith("/"):
            path = "/" + path
        return f"{base}{path}"

    # ---- База данных / DSN ----
    def database_url_asyncpg(self) -> str:
        """
        Возвращает DSN для SQLAlchemy async:
          postgres://   → postgresql+asyncpg://
          postgresql:// → postgresql+asyncpg:// при отсутствии '+asyncpg'.
        """
        if not self.DATABASE_URL:
            raise RuntimeError(
                "DATABASE_URL не задан (нужен DSN Postgres/Neon).",
            )
        url = self.DATABASE_URL
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql+asyncpg://", 1)
        elif url.startswith("postgresql://") and "asyncpg" not in url:
            url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
        return url

    # ---- Decimal / точности ----
    def configure_decimal_context(self) -> None:
        """
        Настраивает глобальный Decimal:
          • precision ≥ EFHC_DECIMALS + запас,
          • округление по умолчанию — ROUND_DOWN.
        """
        ctx = getcontext()
        ctx.prec = max(28, self.EFHC_DECIMALS + 12)
        ctx.rounding = ROUND_DOWN

    def format_kwh(self, value: float | Decimal) -> str:
        """Форматирование kWh для публичного экспорта (KWH_DISPLAY_DECIMALS)."""
        return _quantize_str(value, places=int(self.KWH_DISPLAY_DECIMALS))

    def format_efhc(self, value: float | Decimal) -> str:
        """Форматирование EFHC (строго EFHC_DECIMALS)."""
        return _quantize_str(value, places=int(self.EFHC_DECIMALS))

    # ---- MEMO / SKU ----
    def make_ton_memo_simple_deposit(self, telegram_id: int) -> str:
        """MEMO для простого депозита EFHC: 'EFHC<telegram_id>'."""
        prefix = (self.TON_MEMO_PREFIX_EFHC or "EFHC").strip()
        return f"{prefix}{int(telegram_id)}"

    def make_ton_memo_shop(
        self,
        sku: str,
        qty: int,
        telegram_id: int,
    ) -> str:
        """MEMO для покупки из Shop: 'SKU:<sku>|Q:<qty>|TG:<telegram_id>'."""
        sku_prefix = (self.TON_MEMO_SKU_PREFIX or "SKU").strip()
        return f"{sku_prefix}:{sku}|Q:{int(qty)}|TG:{int(telegram_id)}"

    def is_valid_ton_address(self, addr: str) -> bool:
        """Быстрая проверка формата TON-адреса."""
        return _is_probably_ton_address(addr)

    # ---- CORS ----
    def effective_cors_origins(self) -> List[str]:
        """Возвращает итоговый список CORS-Origin (после парсинга CSV)."""
        return list(self.CORS_ORIGINS or [])

    # ---- Health/диагностика ----
    def assert_required_secrets(self) -> None:
        """
        Мягкая самодиагностика критичных секретов/адресов.
        Печатает WARN, но не падает (самовосстановление за счёт ретраев).
        """
        if not self.TELEGRAM_BOT_TOKEN:
            print("[WARN] BOT_TOKEN не задан — Telegram-бот может не стартовать.")
        if not self.DATABASE_URL:
            print("[WARN] DATABASE_URL не задан — БД будет недоступна.")
        if not self.TON_WALLET_ADDRESS:
            print("[WARN] TON_WALLET_ADDRESS не задан — приём платежей невозможен.")
        if self.TON_WATCHER_ENABLED and not self.TON_API_KEY:
            print(
                "[WARN] TON watcher включён, но TON_API_KEY не задан — "
                "watcher не сможет работать.",
            )

    def debug_dump(self) -> Dict[str, str]:
        """Безопасный дамп ключевых настроек (без секретов) для /health и логов."""
        return {
            "env": self.env_normalized,
            "projectName": self.PROJECT_NAME,
            "version": self.APP_VERSION,
            "apiPrefix": self.API_PREFIX,
            "dbUrlSet": "yes" if bool(self.DATABASE_URL) else "no",
            "corsCount": str(len(self.effective_cors_origins())),
            "isProd": str(self.is_prod),
            "webhookEnabled": str(self.WEBHOOK_ENABLED),
            "webhookBaseUrlSet": "yes" if bool(self.WEBHOOK_BASE_URL) else "no",
            "tonWatcherEnabled": str(self.TON_WATCHER_ENABLED),
            "tonApiUrl": self.TON_API_URL,
            "efhcDecimals": str(self.EFHC_DECIMALS),
            "rateLimit": (
                f"{self.RATE_LIMIT_MAX_REQ}/{self.RATE_LIMIT_WINDOW_SEC}s"
            ),
        }

    # ---- Публичный экспорт для фронтенда (без секретов) ----
    def export_public_frontend_config(self) -> Dict[str, str]:
        """
        Конфиг для фронта:
          • Только строки.
          • Никаких секретов.
          • Точности приведены к канону (8 знаков).
        """
        return {
            "projectName": self.PROJECT_NAME,
            "apiPrefix": self.API_PREFIX,
            "panelPriceEFHC": self.format_efhc(self.PANEL_PRICE_EFHC),
            "panelLifespanDays": str(self.PANEL_LIFESPAN_DAYS),
            "maxActivePanelsPerUser": str(self.MAX_ACTIVE_PANELS_PER_USER),
            "genPerSecBaseKwh": _quantize_str(self.GEN_PER_SEC_BASE_KWH, 8),
            "genPerSecVipKwh": _quantize_str(self.GEN_PER_SEC_VIP_KWH, 8),
            "exchangeRateKwhToEfhc": _quantize_str(self.EFHC_KWH_RATE, 8),
            "lotteryTicketPriceEfhc": self.format_efhc(
                self.LOTTERY_TICKET_PRICE_EFHC,
            ),
            "lotteryMaxTicketsPerUser": str(self.LOTTERY_MAX_TICKETS_PER_USER),
            "tasksEnabled": str(self.TASKS_ENABLED),
            "taskRewardBonusEfhcDefault": self.format_efhc(
                self.TASK_REWARD_BONUS_EFHC_DEFAULT,
            ),
            "supportedLangs": ",".join(self.SUPPORTED_LANGS),
            "defaultLang": self.DEFAULT_LANG,
            "appVersion": self.APP_VERSION,
        }

    # ---- Инициализация рантайма ----
    def ensure_local_artifacts(self) -> None:
        """Создаёт каталог .local_artifacts для dev-режима (кеш/временные файлы)."""
        if self.env_normalized == "local":
            Path(".local_artifacts").mkdir(exist_ok=True)

    def initialize_runtime(self) -> None:
        """
        Единая точка инициализации конфигурации при старте приложения:
          • Приведение DSN БД к async-формату.
          • Настройка Decimal контекста (ROUND_DOWN).
          • Создание локальных артефактов для dev.
          • Мягкая самодиагностика секретов.
        """
        if self.DATABASE_URL:
            _ = self.database_url_asyncpg()

        self.configure_decimal_context()
        self.ensure_local_artifacts()
        self.assert_required_secrets()


# =============================================================================
# Синглтон настроек для всего приложения
# =============================================================================


@lru_cache()
def get_settings() -> Settings:
    """Создаёт и кэширует объект Settings, выполняя initialize_runtime()."""
    settings_obj = Settings()
    settings_obj.initialize_runtime()
    return settings_obj


# Удобный глобальный экспорт:
# from backend.app.core.config_core import settings
settings: Settings = get_settings()

__all__ = ["Settings", "get_settings", "settings"]
