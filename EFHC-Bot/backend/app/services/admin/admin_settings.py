# -*- coding: utf-8 -*-
# backend/app/services/admin/admin_settings.py
# =============================================================================
# EFHC Bot — Системные настройки админ-панели (K/V-хранилище)
# -----------------------------------------------------------------------------
# Назначение модуля:
#   • Централизованное управление параметрами, которые можно менять из админки:
#       - GEN_PER_SEC_BASE_KWH / GEN_PER_SEC_VIP_KWH (скорость генерации в сек)
#       - REFERRAL_DIRECT_BONUS_EFHC (прямой реферальный бонус)
#       - REF_THRESHOLDS (пороговые бонусы рефералок)
#       - LOTTERY_TICKET_PRICE_EFHC / LOTTERY_MAX_TICKETS_PER_USER (дефолты)
#   • Все настройки хранятся в {SCHEMA_ADMIN}.system_settings как key/value.
#   • Любое изменение настройки логируется в admin_logs через AdminLogger.
#
# ИИ-защита / инварианты:
#   • Жёсткий белый список ключей (ALLOWED_SETTINGS) — предотвратить «мусор».
#   • Валидация значений:
#       - Decimal-значения приводятся к 8 знакам после запятой, округление вниз.
#       - Целочисленные настройки требуют положительного значения.
#       - REF_THRESHOLDS проверяются парсером parse_kv_thresholds().
#   • Никакой прямой работы с окружением (.env) — только запись/чтение из БД.
#   • Ошибки валидации → ValueError с понятным сообщением (для UI).
#   • Ошибка логирования не ломает основную операцию (см. AdminLogger).
#
# Ожидаемая схема таблицы (канон):
#   {SCHEMA_ADMIN}.system_settings (
#       key        TEXT PRIMARY KEY,
#       value      TEXT NOT NULL,
#       updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW() AT TIME ZONE 'UTC'
#   );
# =============================================================================

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from pydantic import BaseModel

from sqlalchemy import text
from sqlalchemy.engine import Result
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config_core import get_settings
from backend.app.core.logging_core import get_logger
from backend.app.core.utils_core import (
    quantize_decimal,
    format_decimal_str,
    parse_kv_thresholds,
)

from .admin_logging import AdminLogger

logger = get_logger(__name__)
S = get_settings()

SCHEMA_ADMIN: str = getattr(S, "DB_SCHEMA_ADMIN", "efhc_admin") or "efhc_admin"


# =============================================================================
# DTO для системных настроек
# =============================================================================

class SystemSetting(BaseModel):
    """
    Одна настройка в системе.

    key        — имя настройки (из белого списка);
    value      — строковое значение (после нормализации/валидации);
    updated_at — ISO8601-строка (UTC), когда значение было сохранено.
    """
    key: str
    value: str
    updated_at: Optional[str] = None


# =============================================================================
# Белый список допустимых ключей
# =============================================================================

ALLOWED_SETTINGS: Dict[str, str] = {
    # генерация (секунда)
    "GEN_PER_SEC_BASE_KWH": "Скорость генерации kWh/сек для обычных пользователей",
    "GEN_PER_SEC_VIP_KWH": "Скорость генерации kWh/сек для VIP",

    # рефералка
    "REFERRAL_DIRECT_BONUS_EFHC": "Прямой бонус за каждого реферала (EFHC)",
    "REF_THRESHOLDS": "Пороговые бонусы рефералок в формате '10:1,100:10,1000:100,3000:300,10000:1000'",

    # лотереи (дефолты)
    "LOTTERY_TICKET_PRICE_EFHC": "Цена билета по умолчанию (EFHC)",
    "LOTTERY_MAX_TICKETS_PER_USER": "Лимит билетов на пользователя по умолчанию",
}


# =============================================================================
# Вспомогательная валидация
# =============================================================================

def _normalize_key(key: str) -> str:
    """
    Приводит ключ к верхнему регистру и проверяет, что он разрешён.

    ИИ-защита:
      • Любая попытка сохранить неизвестный ключ → ValueError.
      • Это блокирует «случайные» или ошибочные ключи в БД.
    """
    k = str(key or "").strip().upper()
    if k not in ALLOWED_SETTINGS:
        raise ValueError("Недопустимый ключ настройки")
    return k


def _normalize_value(key: str, value: Any) -> str:
    """
    Нормализует и валидирует значение в зависимости от ключа.
    Возвращает безопасную строку для сохранения в БД.
    """
    key_upper = _normalize_key(key)  # ещё раз проверим/нормализуем
    raw = str(value)

    # Decimal-настройки
    if key_upper in {"GEN_PER_SEC_BASE_KWH", "GEN_PER_SEC_VIP_KWH", "REFERRAL_DIRECT_BONUS_EFHC", "LOTTERY_TICKET_PRICE_EFHC"}:
        try:
            q = quantize_decimal(raw, 8, "DOWN")
        except Exception as e:  # noqa: BLE001
            raise ValueError(f"Значение для {key_upper} должно быть числом") from e

        if q <= 0:
            # для генерации и бонусов/цен нет смысла в нуле или отрицательных значениях
            raise ValueError(f"Значение для {key_upper} должно быть положительным")
        return format_decimal_str(q, 8)

    # Целочисленные настройки
    if key_upper in {"LOTTERY_MAX_TICKETS_PER_USER"}:
        try:
            ivalue = int(raw)
        except Exception as e:  # noqa: BLE001
            raise ValueError(f"Значение для {key_upper} должно быть целым числом") from e
        if ivalue <= 0:
            raise ValueError(f"Значение для {key_upper} должно быть положительным")
        return str(ivalue)

    # Пороговые бонусы рефералок
    if key_upper == "REF_THRESHOLDS":
        # Минимальная проверка формата, например "10:1,100:10"
        try:
            parse_kv_thresholds(raw)
        except Exception as e:  # noqa: BLE001
            raise ValueError("REF_THRESHOLDS должен быть в формате '10:1,100:10,...'") from e
        return raw

    # Базовый случай — строка как есть
    return raw


# =============================================================================
# Сервис системных настроек
# =============================================================================

class SettingsService:
    """
    Сервис работы с системными настройками:

      • get() / mget() — чтение одной или нескольких настроек;
      • set() / mset() — запись значений (только для ключей из ALLOWED_SETTINGS).

    Важные моменты:
      • Все операции записи логируются через AdminLogger (SET_SETTING).
      • Валидация выполняется перед обращением к БД.
      • При отсутствии ключа get() возвращает None.
    """

    # -------------------------------------------------------------------------
    # Чтение
    # -------------------------------------------------------------------------

    @staticmethod
    async def get(db: AsyncSession, key: str) -> Optional[SystemSetting]:
        """
        Возвращает одну настройку по ключу или None, если она ещё не задана.

        ИИ-защита:
          • Ключ нормализуется, но отсутствие записи не считается ошибкой.
        """
        norm_key = _normalize_key(key)
        sql = text(
            f"""
            SELECT key, value, updated_at
            FROM {SCHEMA_ADMIN}.system_settings
            WHERE key = :k
            LIMIT 1
            """
        )
        r: Result = await db.execute(sql, {"k": norm_key})
        row = r.fetchone()
        if not row:
            return None
        upd = getattr(row, "updated_at", None)
        return SystemSetting(
            key=str(getattr(row, "key")),
            value=str(getattr(row, "value")),
            updated_at=upd.isoformat() if hasattr(upd, "isoformat") else (str(upd) if upd is not None else None),
        )

    @staticmethod
    async def mget(db: AsyncSession, keys: Sequence[str]) -> List[SystemSetting]:
        """
        Возвращает список настроек по нескольким ключам.
        Неизвестные ключи будут отфильтрованы (через _normalize_key).
        """
        if not keys:
            return []

        norm_keys: List[str] = []
        for k in keys:
            try:
                norm_keys.append(_normalize_key(k))
            except ValueError:
                # Неизвестный ключ игнорируем (не считаем это критической ошибкой для mget)
                logger.warning("SettingsService.mget: игнорируем неизвестный ключ %r", k)

        if not norm_keys:
            return []

        sql = text(
            f"""
            SELECT key, value, updated_at
            FROM {SCHEMA_ADMIN}.system_settings
            WHERE key = ANY(:keys)
            """
        )
        r: Result = await db.execute(sql, {"keys": norm_keys})
        rows = r.fetchall()

        out: List[SystemSetting] = []
        for row in rows:
            try:
                upd = getattr(row, "updated_at", None)
                out.append(
                    SystemSetting(
                        key=str(getattr(row, "key")),
                        value=str(getattr(row, "value")),
                        updated_at=upd.isoformat() if hasattr(upd, "isoformat") else (str(upd) if upd is not None else None),
                    )
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("SettingsService.mget: пропущена «битая» строка %r: %s", row, e)
                continue
        return out

    # -------------------------------------------------------------------------
    # Запись
    # -------------------------------------------------------------------------

    @staticmethod
    async def set(
        db: AsyncSession,
        *,
        key: str,
        value: Any,
        admin_id: int,
    ) -> None:
        """
        Сохраняет одну настройку.

        Порядок:
          1) Проверяем/нормализуем ключ (белый список).
          2) Валидируем и нормализуем значение.
          3) INSERT ... ON CONFLICT (key) DO UPDATE.
          4) Пишем лог через AdminLogger (SET_SETTING).

        Исключения:
          • ValueError — при недопустимом ключе или некорректном значении.
        """
        norm_key = _normalize_key(key)
        norm_val = _normalize_value(norm_key, value)

        sql = text(
            f"""
            INSERT INTO {SCHEMA_ADMIN}.system_settings (key, value, updated_at)
            VALUES (:k, :v, NOW() AT TIME ZONE 'UTC')
            ON CONFLICT (key) DO UPDATE
            SET value = EXCLUDED.value,
                updated_at = EXCLUDED.updated_at
            """
        )
        await db.execute(sql, {"k": norm_key, "v": norm_val})

        # Логируем изменение настройки (ошибки логирования не пробрасываются)
        await AdminLogger.write(
            db,
            admin_id=admin_id,
            action="SET_SETTING",
            entity="system_settings",
            entity_id=None,
            details=f"{norm_key}={norm_val}",
        )

    @staticmethod
    async def mset(
        db: AsyncSession,
        *,
        items: Dict[str, Any],
        admin_id: int,
    ) -> None:
        """
        Сохраняет несколько настроек подряд.

        ИИ-защита:
          • Каждая настройка валидируется отдельно.
          • Ошибка по одному ключу не блокирует остальные — но выбрасывается
            в конце последняя встретившаяся ошибка. Логи по успешно сохранённым
            ключам всё равно будут записаны.
        """
        last_error: Optional[Exception] = None
        for k, v in items.items():
            try:
                await SettingsService.set(db, key=k, value=v, admin_id=admin_id)
            except Exception as e:  # noqa: BLE001
                logger.warning("SettingsService.mset: ошибка для ключа %r: %s", k, e)
                last_error = e
                continue

        if last_error is not None:
            # Даём знать вызывающему коду, что были проблемы (UI может показать предупреждение).
            raise last_error


__all__ = [
    "SystemSetting",
    "ALLOWED_SETTINGS",
    "SettingsService",
]

