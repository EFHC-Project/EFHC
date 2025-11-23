"""Utility helpers shared across EFHC services.

Все хелперы здесь дают «безопасные по умолчанию» операции: строгая
дата/время в UTC, квантизация денег с отсечением, стабильные ETag и
константное сравнение строк. Комментарии написаны «для чайника», чтобы
любой разработчик мгновенно понимал, что происходит.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Any, Iterable, Sequence

DECIMAL_QUANT = Decimal("0.00000001")


def quantize_decimal(value: Decimal, decimals: int = 8, rounding: str = "DOWN") -> Decimal:
    """Привести Decimal к точности EFHC с отсечением.

    Почему так: по канону EFHC все суммы имеют 8 знаков после запятой и
    всегда округляются вниз, чтобы исключить появление «лишних» монет из
    округления. Экспонента формируется явно, а режим округления выбирается
    по строковому флагу (но для безопасности везде остаётся ROUND_DOWN).
    """

    exponent = Decimal(f"1e-{decimals}")
    rounding_mode = ROUND_DOWN if rounding.upper() == "DOWN" else ROUND_DOWN
    return value.quantize(exponent, rounding=rounding_mode)


def utc_now() -> datetime:
    """Вернуть текущее время в UTC с tzinfo.

    Используем только aware-даты, чтобы избежать ошибок с часовыми поясами
    в БД и при расчётах TTL/ретраев.
    """

    return datetime.now(timezone.utc)


def stable_etag(payload: bytes | str) -> str:
    """Сформировать стабильный ETag (SHA-256) для кэшируемых ответов."""

    data = payload if isinstance(payload, bytes) else payload.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def json_for_etag(data: Any) -> str:
    """Построить детерминированное JSON-представление для ETag.

    Для простоты используем сортировку ключей и компактные разделители, а все
    неизвестные типы приводим к строке. Это гарантирует, что одинаковые данные
    дадут одинаковый ETag даже при другом порядке ключей в словаре.
    """

    return json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)


def constant_time_compare(left: str, right: str) -> bool:
    """Безопасное сравнение строк без утечек по времени."""

    return hmac.compare_digest(left, right)


def cursor_from_items(items: Sequence[object], key_attr: str = "id") -> str | None:
    """Вернуть курсор по последнему элементу выборки.

    Курсор — это строка, обычно `id` последней записи. Если элементов нет,
    возвращаем `None`, чтобы фронт понимал, что пагинация закончилась.
    """

    if not items:
        return None
    last = items[-1]
    return str(getattr(last, key_attr))
