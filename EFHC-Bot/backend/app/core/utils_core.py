# -*- coding: utf-8 -*-
# backend/app/core/utils_core.py
# =============================================================================
# Назначение:
#   • Базовые утилиты уровня "core" без зависимостей от FastAPI/SQLAlchemy.
#   • Работа с Decimal (точность 8 знаков, фиксированное округление).
#   • Безопасные конвертации чисел, форматирование строк.
#   • Время/таймстемпы, хэши/HMAC.
#   • Генерация коротких реф-кодов и идемпотентных ключей.
#
# Канон EFHC:
#   • EFHC и kWh во всех публичных форматах — не более 8 знаков после запятой.
#   • Операции с балансами по умолчанию используют округление DOWN
#     (обрезаем, а не округляем вверх).
#   • Все функции чистые: без сетевых вызовов и без побочных эффектов.
#
# ИИ-защита:
#   • Любые некорректные входные значения (NaN, inf, мусорные строки) не
#     приводят к падению — возвращаются безопасные значения (0 или None).
#   • Утилиты ориентированы на повторное использование в разных слоях
#     (core / services / роутеры), исключая дублирование логики.
# =============================================================================

from __future__ import annotations

import hashlib
import hmac
import secrets
import string
from datetime import datetime, timezone
from decimal import (
    Decimal,
    InvalidOperation,
    ROUND_CEILING,
    ROUND_DOWN,
    ROUND_FLOOR,
    ROUND_HALF_UP,
    getcontext,
)
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

# Глобальный контекст Decimal — не задаём слишком жёстко, оставляем
# разумную общую точность для промежуточных расчётов.
getcontext().prec = 28

NumberLike = Union[str, int, float, Decimal]

_ROUNDING_MAP: Dict[str, str] = {
    "DOWN": ROUND_DOWN,
    "HALF_UP": ROUND_HALF_UP,
    "FLOOR": ROUND_FLOOR,
    "CEILING": ROUND_CEILING,
}

# Алфавит для реф-кодов (без двусмысленных символов O/0).
_DEFAULT_ALPHABET = (
    string.ascii_uppercase.replace("O", "") + string.digits.replace("0", "")
)


# -----------------------------------------------------------------------------
# Decimal helpers
# -----------------------------------------------------------------------------
def decimal_from(value: NumberLike) -> Decimal:
    """
    Безопасно приводит значение к Decimal.

    Особенности:
    • float приводим через str(), чтобы минимизировать бинарные артефакты.
    • Для строк/интов — обычный Decimal(value).
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    return Decimal(value)


def quantize_decimal(
    value: NumberLike,
    decimals: int = 8,
    rounding: str = "DOWN",
) -> Decimal:
    """
    Округляет число до fixed-point с заданной точностью.

    По умолчанию:
    • 8 знаков после запятой;
    • округление DOWN (обрезаем, а не округляем вверх).
    """
    d = decimal_from(value)
    q = Decimal(1).scaleb(-decimals)  # = Decimal("1e-8") при decimals=8
    rounding_mode = _ROUNDING_MAP.get(rounding.upper(), ROUND_DOWN)
    try:
        return d.quantize(q, rounding=rounding_mode)
    except InvalidOperation:
        # Если d = NaN/inf или пришло что-то странное — возвращаем 0
        return Decimal(0).quantize(q, rounding=rounding_mode)


def format_decimal_str(
    value: NumberLike,
    decimals: int = 8,
    trim_trailing_zeros: bool = True,
    rounding: str = "DOWN",
) -> str:
    """
    Возвращает строку с фиксированным числом знаков (или с обрезкой нулей).

    Параметры:
    • decimals — сколько знаков после запятой печатаем;
    • trim_trailing_zeros — обрезать ли хвостовые нули и точку;
    • rounding — режим округления (см. _ROUNDING_MAP).
    """
    d = quantize_decimal(value, decimals=decimals, rounding=rounding)
    s = f"{d:.{decimals}f}"
    if trim_trailing_zeros and "." in s:
        s = s.rstrip("0").rstrip(".")
    return s


def ensure_non_negative(value: NumberLike) -> Decimal:
    """
    Гарантирует, что число не отрицательное.

    Отрицательные значения приводятся к 0 (используется как страховка
    от неожиданных результатов).
    """
    d = decimal_from(value)
    return d if d >= 0 else Decimal(0)


def clamp(
    value: NumberLike,
    min_value: Optional[NumberLike] = None,
    max_value: Optional[NumberLike] = None,
) -> Decimal:
    """
    Ограничивает значение снизу/сверху.

    Параметры:
    • min_value — нижняя граница (если None — не ограничиваем снизу);
    • max_value — верхняя граница (если None — не ограничиваем сверху).
    """
    d = decimal_from(value)
    if min_value is not None:
        min_d = decimal_from(min_value)
        if d < min_d:
            d = min_d
    if max_value is not None:
        max_d = decimal_from(max_value)
        if d > max_d:
            d = max_d
    return d


# Специализированные форматтеры (по умолчанию точность 8, округление DOWN)
def to_efhc_str(value: NumberLike, decimals: int = 8) -> str:
    """
    Форматирование EFHC в строку.

    Канон:
    • EFHC отображается не более чем с 8 знаками после запятой;
    • округление — DOWN.
    """
    return format_decimal_str(
        value,
        decimals=decimals,
        trim_trailing_zeros=True,
        rounding="DOWN",
    )


def to_kwh_str(value: NumberLike, decimals: int = 8) -> str:
    """
    Форматирование kWh в строку (аналогично EFHC).

    Канон:
    • kWh отображается не более чем с 8 знаками после запятой;
    • округление — DOWN.
    """
    return format_decimal_str(
        value,
        decimals=decimals,
        trim_trailing_zeros=True,
        rounding="DOWN",
    )


# -----------------------------------------------------------------------------
# Время / таймстемпы
# -----------------------------------------------------------------------------
def utcnow() -> datetime:
    """Текущее время в UTC с tzinfo=UTC."""
    return datetime.now(tz=timezone.utc)


def unix_ms() -> int:
    """Unix timestamp в миллисекундах (int)."""
    return int(utcnow().timestamp() * 1000)


def parse_iso_datetime(raw: Optional[str]) -> Optional[datetime]:
    """
    Ненавязчивый парсер ISO-даты/времени без зависимостей от веб-фреймворков.

    Возвращает:
    • datetime с tzinfo (если удалось распарсить);
    • None, если строка пустая или некорректная.

    Примеры входа:
    • "2025-08-27"
    • "2025-08-27T12:30:00"
    • "2025-08-27T12:30:00Z"
    """
    if not raw:
        return None
    try:
        candidate = raw
        if (
            "T" not in candidate
            and ":" not in candidate
            and " " not in candidate
        ):
            candidate = f"{candidate}T00:00:00"
        return datetime.fromisoformat(candidate.replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        return None


# -----------------------------------------------------------------------------
# Хэши / HMAC
# -----------------------------------------------------------------------------
def sha256_hex(data: Union[str, bytes]) -> str:
    """
    SHA-256 в hex.

    Строки кодируются как UTF-8 перед хэшированием.
    """
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def hmac_sha256_hex(
    secret: Union[str, bytes],
    message: Union[str, bytes],
) -> str:
    """
    HMAC-SHA256 в hex.

    Секрет и сообщение при необходимости кодируются как UTF-8.
    """
    if isinstance(secret, str):
        secret = secret.encode("utf-8")
    if isinstance(message, str):
        message = message.encode("utf-8")
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


# -----------------------------------------------------------------------------
# Реф-коды и идемпотентность
# -----------------------------------------------------------------------------
def gen_ref_code(
    length: int = 8,
    alphabet: str = _DEFAULT_ALPHABET,
) -> str:
    """
    Генерация короткого реф-кода.

    Особенности:
    • Используются только A–Z и 1–9 (без 0/O для читаемости).
    """
    return "".join(secrets.choice(alphabet) for _ in range(length))


def gen_idempotency_key(
    prefix: str = "idem",
    length: int = 24,
) -> str:
    """
    Генерация идемпотентного ключа вида: "<prefix>_<random>".

    Используется:
    • Для безопасных повторных запросов (POST/PUT/PATCH/DELETE);
    • Для внутренних сервисов, которые инициируют денежные операции.
    """
    token = secrets.token_urlsafe(length)
    return f"{prefix}_{token}"


# -----------------------------------------------------------------------------
# Коллекции / парсеры
# -----------------------------------------------------------------------------
def unique(seq: Sequence[Any]) -> List[Any]:
    """
    Возвращает элементы без повторов, сохраняя порядок первого появления.
    """
    seen: set[Any] = set()
    out: List[Any] = []
    for item in seq:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def parse_csv_list(s: Optional[str]) -> List[str]:
    """
    Парсер CSV-строки в список.

    Особенности:
    • Обрезает пробелы;
    • Пропускает пустые элементы.
    """
    if not s:
        return []
    return [part.strip() for part in s.split(",") if part.strip()]


def parse_kv_thresholds(s: Optional[str]) -> List[Tuple[int, Decimal]]:
    """
    Парсер строк вида "10:1,100:10" → [(10, Decimal("1")), (100, Decimal("10"))].

    Использование:
    • Пороговые бонусы (рефералка, ачивки и т.п.);
    • Любые схемы вида "количество:награда".
    """
    if not s:
        return []

    items: List[Tuple[int, Decimal]] = []
    for chunk in s.split(","):
        chunk = chunk.strip()
        if not chunk or ":" not in chunk:
            continue
        left, right = chunk.split(":", 1)
        try:
            threshold = int(left.strip())
            amount = decimal_from(right.strip())
        except Exception:  # noqa: BLE001
            # Некорректный элемент — тихо пропускаем.
            continue
        items.append((threshold, amount))

    items.sort(key=lambda pair: pair[0])
    return items


__all__ = [
    # decimal
    "decimal_from",
    "quantize_decimal",
    "format_decimal_str",
    "ensure_non_negative",
    "clamp",
    "to_efhc_str",
    "to_kwh_str",
    # time
    "utcnow",
    "unix_ms",
    "parse_iso_datetime",
    # crypto
    "sha256_hex",
    "hmac_sha256_hex",
    # ids
    "gen_ref_code",
    "gen_idempotency_key",
    # parsing / collections
    "unique",
    "parse_csv_list",
    "parse_kv_thresholds",
]
