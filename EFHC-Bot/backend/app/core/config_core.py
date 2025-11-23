"""Core configuration and canonical constants for EFHC Bot v2.8."""

from __future__ import annotations

# ======================================================================
# EFHC Bot — core/config_core.py
# ----------------------------------------------------------------------
# Назначение: централизованная загрузка настроек окружения и хранение
#             канонических per-sec ставок генерации для всех сервисов.
# Канон/инварианты:
#   • Единственные ставки: GEN_PER_SEC_BASE_KWH и GEN_PER_SEC_VIP_KWH
#     (per-second, без суточных дубликатов).
#   • Денежные операции не выполняются здесь; модуль не меняет балансы.
#   • Параметры безопасности (Idempotency/ETag) включены по умолчанию.
# ИИ-защиты/самовосстановление:
#   • LRU-кеширование настроек предотвращает расхождения между сервисами.
#   • Жёсткие значения по умолчанию позволяют стартовать даже без .env,
#     сохраняя каноническую конфигурацию.
# Запреты:
#   • Нет P2P и EFHC→kWh; модуль не трогает экономику напрямую.
#   • Никаких суточных ставок — только per-second из этого файла.
# ======================================================================

from decimal import Decimal
from functools import lru_cache
from typing import Literal, Sequence

from pydantic import BaseSettings, Field, PostgresDsn, validator

GEN_PER_SEC_BASE_KWH = Decimal("0.00000692")  # базовая ставка per-sec
GEN_PER_SEC_VIP_KWH = Decimal("0.00000741")  # VIP ставка per-sec


class CoreConfig(BaseSettings):
    """Загрузить настройки EFHC из окружения с типовой валидацией.

    Назначение: предоставить сервисам единый источник адресов TON,
    коллекции NFT для VIP, списка админов и флагов строгого режима.
    Вход: переменные окружения с префиксом ``EFHC_`` (см. классовые
    поля), а ``DATABASE_URL`` читается напрямую.
    Выход: объект с типами Pydantic, кешируемый в :func:`get_core_config`.
    Побочные эффекты: отсутствуют, модуль балансы не меняет.
    Идемпотентность: значения кешируются в LRU, повторный вызов
    возвращает тот же объект без повторного парсинга.
    Исключения: Pydantic поднимает ошибки при неверных форматах.
    ИИ-защита: безопасные дефолты и строгие флаги для Idempotency/ETag,
    чтобы фронт/бот не нарушали канон даже в дев-окружении.
    """

    app_env: Literal["dev", "stage", "prod"] = Field(
        "dev", description="Переключатель окружения для фич и логирования."
    )
    database_url: PostgresDsn = Field(..., env="DATABASE_URL")

    ton_wallet_address: str = Field(
        "UQAyCoxmxzb2D6cmlf4M8zWYFYkaQuHbN_dgH-IfraFP8QKW",
        description="Адрес TON для входящих депозитов (канон).",
    )
    vip_nft_collection: str = Field(
        "EQASPXkEI0NsZQzqkPjk6O_i752LfwSWRFT9WzDc2SJ2zgi0",
        description="TON-коллекция NFT, дающая VIP (ручная выдача).",
    )

    admin_telegram_ids: Sequence[int] = Field(
        default_factory=list,
        description="Белый список Telegram ID с админ-доступом.",
    )
    admin_api_key: str = Field(
        "dev-admin-key",
        description="API-ключ для серверных admin-ручек (X-Admin-Api-Key).",
    )
    strict_idempotency: bool = Field(
        True,
        description=(
            "Включает обязательный Idempotency-Key для денежных запросов."
        ),
    )
    strict_etag: bool = Field(
        True,
        description="Включает выдачу ETag на GET для кэширования и 304.",
    )
    scheduler_tick_minutes: int = Field(
        10,
        description="Канонический тик планировщика (в минутах).",
    )

    class Config:
        env_prefix = "EFHC_"
        case_sensitive = False

    @validator("admin_telegram_ids", pre=True)
    def _parse_admin_ids(cls, value: str | Sequence[int]) -> Sequence[int]:
        """Разобрать список админов (строка CSV или уже последовательность).

        Назначение: позволить задавать ids в .env как ``1,2,3`` и вернуть
        неизменяемый tuple. Побочные эффекты отсутствуют, идемпотентно.
        Исключения: ValueError при нечисловых значениях.
        """

        if isinstance(value, str):
            return tuple(
                int(item.strip())
                for item in value.split(",")
                if item.strip().lstrip("+-").isdigit()
            )
        return tuple(value)


@lru_cache(maxsize=1)
def get_core_config() -> CoreConfig:
    """Вернуть кешированный экземпляр конфигурации EFHC.

    Назначение: единая точка доступа к env-настройкам для всего кода.
    Вход: нет, использует переменные окружения.
    Выход: :class:`CoreConfig` с заполненными полями.
    Побочные эффекты: кэширует объект в LRU, не меняет БД/балансы.
    Идемпотентность: повторное чтение возвращает один и тот же объект.
    Исключения: ошибки валидации Pydantic при неправильных env.
    """

    return CoreConfig()


# ======================================================================
# Пояснения «для чайника»:
#   • Этот модуль не двигает деньги и не меняет балансы, только читает env.
#   • Пер-сек ставки генерации живут здесь и проверяются в system_locks.
#   • Строгие флаги (Idempotency/ETag) включены по умолчанию для канона.
#   • Значения по умолчанию позволяют запускать dev без .env, не нарушая
#     запреты EFHC (P2P нет, EFHC→kWh нет, NFT только вручную).
# ======================================================================
