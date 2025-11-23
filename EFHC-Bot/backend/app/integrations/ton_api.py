"""TON API integration helpers.

Здесь живёт детерминированный парсер MEMO и тонкий клиент с таймаутами.
Он не выполняет реальные сети в тестовой среде, но предоставляет интерфейс
для дальнейшей подстановки HTTP-запросов.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Final, Literal

import httpx

from ..core.logging_core import get_logger
from ..core.utils_core import quantize_decimal

logger = get_logger(__name__)

MEMO_DIRECT_RE: Final = re.compile(r"^EFHC(?P<tgid>\d+)$")
MEMO_PACKAGE_RE: Final = re.compile(r"^SKU:EFHC\|Q:(?P<qty>\d+)\|TG:(?P<tgid>\d+)$")
MEMO_VIP_RE: Final = re.compile(r"^SKU:NFT_VIP\|Q:1\|TG:(?P<tgid>\d+)$")


@dataclass(slots=True)
class ParsedMemo:
    """Результат парсинга MEMO для живого кредитования."""

    kind: Literal["direct", "package", "vip"]
    telegram_id: int
    quantity: Decimal


class TonAPIClient:
    """Мини-клиент TON API с защитой по таймаутам."""

    def __init__(self, timeout_seconds: float = 5.0):
        self.timeout = timeout_seconds

    async def get_transaction(self, endpoint: str) -> dict:
        """Выполнить безопасный GET-запрос к TON API."""

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(endpoint)
            response.raise_for_status()
            return response.json()


def parse_memo(memo: str) -> ParsedMemo:
    """Детерминированно распарсить MEMO в соответствии с каноном EFHC."""

    if match := MEMO_DIRECT_RE.match(memo):
        telegram_id = int(match.group("tgid"))
        return ParsedMemo(kind="direct", telegram_id=telegram_id, quantity=Decimal("0"))

    if match := MEMO_PACKAGE_RE.match(memo):
        telegram_id = int(match.group("tgid"))
        quantity = quantize_decimal(Decimal(match.group("qty")))
        return ParsedMemo(kind="package", telegram_id=telegram_id, quantity=quantity)

    if match := MEMO_VIP_RE.match(memo):
        telegram_id = int(match.group("tgid"))
        return ParsedMemo(kind="vip", telegram_id=telegram_id, quantity=Decimal("0"))

    raise ValueError(f"Unknown MEMO format: {memo}")
