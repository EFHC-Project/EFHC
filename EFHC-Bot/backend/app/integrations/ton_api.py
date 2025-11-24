# -*- coding: utf-8 -*-
# backend/app/integrations/ton_api.py
# =============================================================================
# EFHC Bot — Интеграция с TonAPI (детерминированный MEMO-парсер и клиент)
# -----------------------------------------------------------------------------
# Назначение:
#   • Детально парсит MEMO по канону EFHC (EFHC<tgid>, SKU:EFHC|Q:x|TG:y,
#     SKU:NFT_VIP|Q:1|TG:y).
#   • Забирает входящие транзакции по адресу проекта с таймаутами и fallback
#     по списку эндпоинтов TonAPI, преобразуя их в DTO TonAPIEvent.
#   • Не меняет балансы и не выполняет денежные операции — только получает
#     данные для watcher_service.
#
# Канон/инварианты:
#   • Курс фиксирован: 1 EFHC = 1 kWh. Обратная конверсия EFHC→kWh отсутствует.
#   • P2P запрещён; любые деньги двигает только банковский сервис
#     (transactions_service) в других модулях.
#   • MEMO-форматы ограничены каноном: EFHC<tgid>, SKU:EFHC|Q:…|TG:…,
#     SKU:NFT_VIP|Q:1|TG:…; любые иные считаются ошибкой.
#   • Все суммы нормализуются через Decimal с 8 знаками (quantize_decimal).
#
# ИИ-защиты/самовосстановление:
#   • Fallback по нескольким базовым URL TonAPI: при сетевой ошибке клиент
#     мягко переключается на следующий эндпоинт, логируя предупреждение.
#   • Таймауты httpx защищают от зависания запросов; ошибки не валят процесс —
#     бросается исключение, которое watcher_service оборачивает в retry-статусы.
#   • MEMO-парсер детерминированный, без «магии» и побочных эффектов.
#
# Запреты:
#   • Модуль не вызывает банковский сервис и не изменяет балансы.
#   • Нет автодоставки NFT и никаких EFHC→kWh/P2P операций.
# =============================================================================
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Iterable, List, Sequence

import httpx

from backend.app.core.config_core import get_settings
from backend.app.core.logging_core import get_logger
from backend.app.core.utils_core import quantize_decimal

logger = get_logger(__name__)
settings = get_settings()

# -----------------------------------------------------------------------------
# Канонические регулярные выражения MEMO
# -----------------------------------------------------------------------------
_MEMO_DIRECT = re.compile(r"^EFHC(?P<tgid>\d{1,20})$")
_MEMO_SKU_EFHC = re.compile(r"^SKU:EFHC\|Q:(?P<qty>\d{1,12})\|TG:(?P<tgid>\d{1,20})$")
_MEMO_SKU_NFT = re.compile(r"^SKU:NFT_VIP\|Q:1\|TG:(?P<tgid>\d{1,20})$")

# -----------------------------------------------------------------------------
# Базовые TonAPI эндпоинты (fallback список)
# -----------------------------------------------------------------------------
_DEFAULT_BASE_URLS: tuple[str, ...] = (
    getattr(settings, "TON_API_URL", "").rstrip("/") or "https://tonapi.io/v2",
    "https://toncenter.com/api/v2",
)


@dataclass(slots=True)
class ParsedMemo:
    """Результат парсинга MEMO по канону EFHC."""

    kind: str  # direct | package | vip | bad
    telegram_id: int | None
    quantity: Decimal | None


@dataclass(slots=True)
class TonAPIEvent:
    """DTO входящей транзакции из TonAPI.

    Поля совпадают с ожидаемыми watcher_service: tx_hash, from/to, amount (TON),
    memo, utime (unix time).
    """

    tx_hash: str
    from_address: str
    to_address: str
    amount: Decimal
    memo: str
    utime: int


class TonAPIError(RuntimeError):
    """Исключение TonAPI (сохраняем для чёткой диагностики)."""


def parse_memo(memo: str) -> ParsedMemo:
    """Детерминированно распарсить MEMO в соответствии с каноном EFHC.

    Назначение: дать watcher_service тип операции и идентификатор пользователя.
    Вход: строка MEMO из транзакции TON.
    Выход: ParsedMemo(kind, telegram_id, quantity).
    Исключения: ValueError при неизвестном формате.
    """

    memo_clean = (memo or "").strip()
    if m := _MEMO_DIRECT.match(memo_clean):
        return ParsedMemo(kind="direct", telegram_id=int(m.group("tgid")), quantity=None)
    if m := _MEMO_SKU_EFHC.match(memo_clean):
        return ParsedMemo(
            kind="package",
            telegram_id=int(m.group("tgid")),
            quantity=quantize_decimal(Decimal(m.group("qty"))),
        )
    if m := _MEMO_SKU_NFT.match(memo_clean):
        return ParsedMemo(kind="vip", telegram_id=int(m.group("tgid")), quantity=None)
    raise ValueError(f"Unknown MEMO format: {memo_clean}")


def _nanoton_to_ton(value: int | str | float | Decimal) -> Decimal:
    """Переводит нанотоны в TON с Decimal(8) округлением вниз."""

    return quantize_decimal(Decimal(str(value)) / Decimal(1_000_000_000))


def _extract_comment(event: dict[str, Any]) -> str:
    """Аккуратно вытянуть комментарий/MEMO из TonAPI события."""

    in_msg = event.get("in_msg") or {}
    # Возможные источники текста: comment, decoded_body.text, body
    return (
        (in_msg.get("comment") or "")
        or str((in_msg.get("decoded_body") or {}).get("text") or "")
        or str(in_msg.get("body") or "")
    ).strip()


class TonAPIClient:
    """Лёгкий клиент TonAPI с fallback и таймаутами.

    Модуль не выполняет денежных операций и не изменяет балансы — только
    получает сырые события для дальнейшей идемпотентной обработки watcher_service.
    """

    def __init__(
        self,
        *,
        base_urls: Sequence[str] | None = None,
        api_keys: Sequence[str] | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        self.base_urls: List[str] = [
            url.rstrip("/") for url in (base_urls or _DEFAULT_BASE_URLS) if url
        ]
        self.api_keys: List[str] = [key for key in (api_keys or [getattr(settings, "TON_API_KEY", None)]) if key]
        self.timeout_seconds = timeout_seconds

    async def _request_json(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        """Выполнить GET с таймаутом и вернуть JSON или бросить TonAPIError."""

        headers = {"Accept": "application/json"}
        if self.api_keys:
            headers["Authorization"] = f"Bearer {self.api_keys[0]}"
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.get(url, params=params, headers=headers)
            response.raise_for_status()
            return response.json()

    async def get_incoming_payments(
        self, *, to_address: str, limit: int = 200, since_utime: int | None = None
    ) -> list[TonAPIEvent]:
        """Получить входящие платежи на указанный TON-адрес.

        Вход: to_address (кошелёк проекта), limit, since_utime (unixtime, опционально).
        Выход: список TonAPIEvent для дальнейшей идемпотентной обработки.
        ИИ-защита: fallback по base_urls, таймауты httpx, логирование с extra.
        Исключения: TonAPIError, если все эндпоинты недоступны.
        """

        errors: list[str] = []
        for base in self.base_urls:
            url = f"{base}/blockchain/getTransactions" if "toncenter" in base else f"{base}/accounts/{to_address}/transactions"
            params = {"account": to_address, "limit": int(limit)}
            if since_utime is not None:
                params["start_time"] = int(since_utime)
                params["start_utime"] = int(since_utime)
            try:
                payload = await self._request_json(url, params)
                return self._parse_events(payload, to_address)
            except Exception as exc:  # noqa: BLE001
                errors.append(str(exc))
                logger.warning(
                    "[TonAPI] endpoint failed",
                    extra={"base": base, "error": str(exc)},
                )
                continue
        raise TonAPIError(f"All TonAPI endpoints failed: {errors}")

    def _parse_events(self, payload: dict[str, Any], to_address: str) -> list[TonAPIEvent]:
        """Преобразовать ответ TonAPI в список TonAPIEvent."""

        events_raw: Iterable[dict[str, Any]] = payload.get("events") or payload.get("transactions") or []
        events: list[TonAPIEvent] = []
        for ev in events_raw:
            try:
                events.append(self._parse_event(ev, to_address))
            except Exception as exc:  # noqa: BLE001
                logger.warning("[TonAPI] skip event", extra={"error": str(exc), "raw": str(ev)[:512]})
                continue
        return events

    def _parse_event(self, ev: dict[str, Any], to_address: str) -> TonAPIEvent:
        """Распарсить одно событие TonAPI в DTO TonAPIEvent."""

        in_msg = ev.get("in_msg") or ev.get("inMessage") or {}
        from_addr = str(in_msg.get("source") or in_msg.get("source_address") or "")
        dest_addr = str(in_msg.get("destination") or in_msg.get("destination_address") or to_address)
        tx_hash = str(ev.get("hash") or ev.get("transaction_id") or ev.get("id") or "")
        utime = int(ev.get("utime") or ev.get("timestamp") or ev.get("time") or 0)
        value_raw = (
            in_msg.get("value")
            or in_msg.get("amount")
            or ev.get("value")
            or ev.get("amount")
            or 0
        )
        memo = _extract_comment(ev)
        amount_ton = _nanoton_to_ton(value_raw)
        if not tx_hash:
            raise TonAPIError("Event without tx_hash")
        return TonAPIEvent(
            tx_hash=tx_hash,
            from_address=from_addr,
            to_address=dest_addr,
            amount=amount_ton,
            memo=memo,
            utime=utime,
        )


__all__ = [
    "ParsedMemo",
    "TonAPIEvent",
    "TonAPIClient",
    "TonAPIError",
    "parse_memo",
]


# =============================================================================
# Пояснения «для чайника»:
#   • Этот модуль только читает TonAPI и парсит MEMO; деньги не двигает.
#   • MEMO-форматы жёстко ограничены каноном: EFHC<tgid>, SKU:EFHC|Q:x|TG:y,
#     SKU:NFT_VIP|Q:1|TG:y; остальное — ошибка.
#   • Таймауты и fallback по base_urls помогают не «ронять» watcher при сбоях.
#   • Значения сумм конвертируются из нанотонов в TON с Decimal(8) округлением
#     вниз через quantize_decimal.
#   • Любые денежные операции выполняются позже через transactions_service,
#     а этот модуль только готовит DTO для идемпотентной обработки.
# =============================================================================
