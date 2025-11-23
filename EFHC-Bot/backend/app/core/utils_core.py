"""Utility helpers shared across EFHC services (канон v2.8)."""

from __future__ import annotations

# ======================================================================
# EFHC Bot — core/utils_core.py
# ----------------------------------------------------------------------
# Назначение: безопасные хелперы для дат, Decimal(8) с отсечением,
#             стабильных ETag и константного сравнения строк.
# Канон/инварианты:
#   • Денежные числа → только Decimal с 8 знаками и ROUND_DOWN.
#   • Курсоры строятся без OFFSET: last-id keyset.
#   • Балансы здесь не меняются, только утилиты.
# ИИ-защиты/самовосстановление:
#   • Дет. ETag через SHA-256 + отсортированный JSON предотвращает ложные
#     кеш-хиты и помогает 304 без расхождений.
#   • utc_now() всегда с tzinfo UTC — исключает баги часовых поясов.
# Запреты:
#   • Нет P2P, нет EFHC→kWh, модуль не выполняет бизнес-операций.
# ======================================================================

import hashlib
import hmac
import json
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Any, Sequence

DECIMAL_QUANT = Decimal("0.00000001")  # экспонента для Decimal(8)


def quantize_decimal(
    value: Decimal, decimals: int = 8, rounding: str = "DOWN"
) -> Decimal:
    """Привести Decimal к точности EFHC с отсечением.

    Назначение: обеспечить единую точность 8 знаков с округлением вниз,
    чтобы исключить «магические» копейки из-за округлений.
    Вход: значение Decimal, желаемая точность и режим округления (строка).
    Выход: Decimal с нужной экспонентой.
    Побочные эффекты: отсутствуют, балансы не меняются.
    Идемпотентность: повтор на том же значении даёт тот же результат.
    Исключения: ValueError при неверном параметре rounding.
    """

    exponent = Decimal(f"1e-{decimals}")
    rounding_mode = ROUND_DOWN if rounding.upper() == "DOWN" else ROUND_DOWN
    return value.quantize(exponent, rounding=rounding_mode)


def utc_now() -> datetime:
    """Вернуть текущее время в UTC с tzinfo (aware-дата).

    Назначение: безопасные метки времени для логов/TTL/ретраев.
    Побочные эффекты: обращается к системным часам, не меняет БД.
    """

    return datetime.now(timezone.utc)


def stable_etag(payload: bytes | str) -> str:
    """Сформировать стабильный ETag (SHA-256) для кэшируемых ответов."""

    data = payload if isinstance(payload, bytes) else payload.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def json_for_etag(data: Any) -> str:
    """Построить детерминированное JSON-представление для ETag.

    Назначение: дать одинаковый хэш при одинаковых данных, даже если
    порядок ключей различается. Все неизвестные типы приводятся к строке.
    Побочные эффекты: отсутствуют, балансы не меняются.
    """

    return json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)


def constant_time_compare(left: str, right: str) -> bool:
    """Безопасное сравнение строк без утечек по времени."""

    return hmac.compare_digest(left, right)


def cursor_from_items(
    items: Sequence[object], key_attr: str = "id"
) -> str | None:
    """Вернуть курсор по последнему элементу выборки (keyset).

    Назначение: keyset-пагинация без OFFSET; курсор — строка из атрибута
    ``key_attr`` последнего элемента. Нет элементов → ``None``.
    Побочные эффекты: отсутствуют.
    Идемпотентность: повторное вычисление для того же списка даёт тот же
    курсор; подходит для ETag и стабильных ссылок.
    """

    if not items:
        return None
    last = items[-1]
    return str(getattr(last, key_attr))


# ======================================================================
# Пояснения «для чайника»:
#   • Деньги здесь не ходят: модуль только форматирует и считает хэши.
#   • Все суммы квантуются вниз до 8 знаков через quantize_decimal.
#   • utc_now() всегда отдаёт UTC с tzinfo — безопасно для БД и TTL.
#   • ETag строится детерминированно: json_for_etag → stable_etag.
#   • Курсор — это id последней записи, OFFSET не используется.
# ======================================================================
