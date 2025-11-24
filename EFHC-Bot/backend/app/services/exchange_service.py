# -*- coding: utf-8 -*-
# backend/app/services/exchange_service.py
# =============================================================================
# EFHC Bot — Обмен доступной энергии (kWh) на EFHC по фиксированному курсу 1:1
# -----------------------------------------------------------------------------
# Назначение файла:
#   • Предоставляет высокоуровневые функции обмена users.available_kwh → EFHC.
#   • Гарантирует соблюдение канона: только однонаправленный обмен (kWh → EFHC),
#     обратный путь ЗАПРЕЩЁН жёстко.
#   • Делегирует все денежные операции ЕДИНОМУ банковскому сервису
#     (backend/app/services/transactions_service.py) с идемпотентностью и
#     запретом «минуса» у пользователей.
#   • Содержит «ИИ»-защиту уровня сервиса: валидации, дружелюбные ошибки,
#     вспомогательные проверки и «самоподсказки» для роутов/UI.
#
# Важные инварианты канона:
#   1) Курс внутренний фиксированный: 1 EFHC = 1 kWh (на уровне бота — неизменный).
#   2) Обмен только в одну сторону: kWh → EFHC. Любая попытка обратной конверсии
#      должна немедленно отклоняться ошибкой уровня сервиса.
#   3) Пользователь НИКОГДА не уходит в минус ни по EFHC, ни по kWh.
#      Банк может уйти в минус (операции не блокируются).
#   4) Идемпотентность обязательна: любой обмен сопровождается уникальным
#      idempotency_key (например, "exchange:<user_id>:<timestamp>:<uuid>").
#   5) Все суммы — Decimal с 8 знаками после запятой (округление ВНИЗ) через deps.d8().
#
# Для чайника:
#   • Этот модуль — «диспетчер обмена». Он проверяет корректность запроса,
#     а затем вызывает transactions_service.exchange_kwh_to_efhc(...) — это
#     единственная функция, которая реально меняет EFHC-балансы в БД.
#   • Если вы строите HTTP-эндпоинт, используйте request_exchange(...) и
#     отдавайте его результат в JSON.
#   • Для UI можно позвать preview_exchange(...) — там безопасно узнаете,
#     сколько максимум можно обменять прямо сейчас (не делает списаний).
# =============================================================================

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, Optional, Tuple

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config_core import get_settings
from backend.app.core.logging_core import get_logger
from backend.app.deps import d8
from backend.app.services.transactions_service import (
    exchange_kwh_to_efhc as bank_exchange_kwh_to_efhc,
)

logger = get_logger(__name__)
settings = get_settings()
SCHEMA = getattr(settings, "DB_SCHEMA_CORE", "efhc_core")

# -----------------------------------------------------------------------------
# Точность и курс (канон)
# -----------------------------------------------------------------------------

EFHC_DECIMALS: int = int(getattr(settings, "EFHC_DECIMALS", 8) or 8)

# Фиксированный курс 1:1 (канон). Храним для явной проверки/health.
KWH_TO_EFHC_RATE = Decimal(str(getattr(settings, "KWH_TO_EFHC_RATE", "1.0") or "1.0"))
if KWH_TO_EFHC_RATE != Decimal("1"):
    # Каноническая защита: даже если в .env кто-то подменил — не позволяем.
    logger.warning("KWH_TO_EFHC_RATE в .env не равен 1. Принудительно применяем 1:1 (канон).")
    KWH_TO_EFHC_RATE = Decimal("1")

# -----------------------------------------------------------------------------
# Специальные ошибки сервиса (для маршрутов/обработчиков)
# -----------------------------------------------------------------------------

class ExchangeError(Exception):
    """Базовая ошибка обмена (для дружелюбного сообщения пользователю)."""


class ExchangeValidationError(ExchangeError):
    """Ошибка валидации запроса (некорректные параметры)."""


class ExchangeDirectionForbidden(ExchangeError):
    """Попытка выполнить запрещённую операцию (EFHC → kWh)."""


class ExchangeInsufficientEnergy(ExchangeError):
    """Недостаточно доступной энергии для обмена."""


class ExchangeIdempotencyRequired(ExchangeError):
    """Отсутствует idempotency_key при strict-режиме или некорректный ключ."""


# -----------------------------------------------------------------------------
# Внутренние хелперы чтения пользователя
# -----------------------------------------------------------------------------

async def _user_energy_snapshot(db: AsyncSession, user_id: int) -> Dict[str, Decimal]:
    """
    Возвращает снимок энергетического состояния пользователя:
      • available_kwh — доступно для обмена (как хранится в users.available_kwh),
      • total_generated_kwh — общая генерация (для информации/статистики).
    Здесь БЕЗ блокировки (предпросмотр).
    """
    row = await db.execute(
        text(
            f"""
            SELECT COALESCE(available_kwh,0)       AS available_kwh,
                   COALESCE(total_generated_kwh,0) AS total_generated_kwh
              FROM {SCHEMA}.users
             WHERE id = :id
            """
        ),
        {"id": int(user_id)},
    )
    r = row.fetchone()
    if not r:
        raise ExchangeValidationError("Пользователь не найден")
    return {
        "available_kwh": d8(r[0]),
        "total_generated_kwh": d8(r[1]),
    }


# -----------------------------------------------------------------------------
# Публичные функции сервиса
# -----------------------------------------------------------------------------

@dataclass
class ExchangePreview:
    ok: bool
    available_kwh: Decimal
    max_exchangeable_kwh: Decimal
    rate_kwh_to_efhc: Decimal = Decimal("1")
    detail: str = ""


async def preview_exchange(db: AsyncSession, *, user_id: int) -> ExchangePreview:
    """
    Безопасно возвращает, сколько энергии доступно к обмену прямо сейчас.
    Ничего не меняет в БД, пригодно для UI/подсказок.
    """
    snap = await _user_energy_snapshot(db, user_id)
    avail = d8(snap["available_kwh"])
    return ExchangePreview(
        ok=True,
        available_kwh=avail,
        max_exchangeable_kwh=avail,  # курс 1:1, так что max = avail
        rate_kwh_to_efhc=KWH_TO_EFHC_RATE,
        detail="К обмену доступно только available_kwh (канон).",
    )


@dataclass
class ExchangeResult:
    ok: bool
    exchanged_kwh: Decimal
    credited_efhc: Decimal
    new_main_balance: Decimal
    new_available_kwh: Decimal
    detail: str = ""
    idempotency_key: Optional[str] = None


async def request_exchange(
    db: AsyncSession,
    *,
    user_id: int,
    amount_kwh: Any,
    idempotency_key: Optional[str],
) -> ExchangeResult:
    """
    Выполняет обмен available_kwh → EFHC по курсу 1:1.

    Параметры:
      • user_id        — внутренний ID пользователя (как в таблице users.id).
      • amount_kwh     — сколько энергии обменять (Decimal-совместимое).
      • idempotency_key — ОБЯЗАТЕЛЬНЫЙ уникальный ключ операции.
        Если ключ повторится, банковский слой обработает операцию идемпотентно.

    Важно:
      • amount_kwh > 0.
      • Никаких «обратных» полей (EFHC→kWh) тут нет и быть не может.
    """
    # 1) Идемпотентность на стороне банка требует наличия ключа
    if not idempotency_key or not str(idempotency_key).strip():
        # Жёсткий канон: ключ обязателен — не генерируем автоматически
        raise ExchangeIdempotencyRequired("Отсутствует idempotency_key (обязателен по канону)")

    # 2) Валидация суммы
    amt = d8(amount_kwh)
    if amt <= 0:
        raise ExchangeValidationError("Сумма обмена должна быть больше 0")

    # 3) Предварительная проверка доступности энергии (для дружелюбной ошибки в UI)
    snap = await _user_energy_snapshot(db, user_id)
    if snap["available_kwh"] < amt:
        # Банк тоже защитит EFHC-баланс от «минуса», но заранее даём понятное сообщение
        raise ExchangeInsufficientEnergy(
            f"Недостаточно доступной энергии: доступно {snap['available_kwh']}, запрошено {amt}"
        )

    # 4) Денежный шаг: идём в банковский сервис (там все блокировки/журнал EFHC).
    #    ВАЖНО:
    #      • В банковском сервисе меняются ТОЛЬКО EFHC-балансы и банк.
    #      • Учёт энергии (available_kwh) ведётся отдельными компонентами
    #        (генерация/агрегатор), здесь мы только проверяем лимит и считаем
    #        «теоретическое» новое значение для UI.
    try:
        tx_result = await bank_exchange_kwh_to_efhc(
            db=db,
            user_id=int(user_id),
            amount_kwh=amt,
            reason="exchange_kwh_to_efhc",  # виден в efhc_transfers_log
            idempotency_key=str(idempotency_key),
        )
    except Exception as e:
        # Переводим необработанные ошибки в дружественные для UI
        msg = f"Не удалось выполнить обмен: {type(e).__name__}: {e}"
        logger.warning("exchange_service.request_exchange failed (user=%s): %s", user_id, msg)
        raise ExchangeError(msg)

    # 5) «Предполагаемое» новое значение доступной энергии для ответа/UI.
    #    Реальный учёт available_kwh может вестись планировщиком по логам.
    theoretical_new_avail = d8(snap["available_kwh"] - amt)
    if theoretical_new_avail < Decimal("0"):
        # Жёсткая защита от программной ошибки/гонки
        logger.warning(
            "exchange_service: theoretical_new_avail < 0 (user=%s, before=%s, amt=%s)",
            user_id,
            snap["available_kwh"],
            amt,
        )
        theoretical_new_avail = Decimal("0")

    return ExchangeResult(
        ok=True,
        exchanged_kwh=amt,
        credited_efhc=amt,  # курс 1:1
        new_main_balance=d8(tx_result.user_main_balance),
        new_available_kwh=theoretical_new_avail,
        idempotency_key=str(idempotency_key),
        detail="Обмен выполнен (kWh→EFHC 1:1).",
    )


# -----------------------------------------------------------------------------
# Жёсткий запрет обратной конверсии (EFHC → kWh)
# -----------------------------------------------------------------------------

def forbid_reverse_exchange(*args, **kwargs) -> None:
    """
    Любая попытка добавить функцию «обратной» конверсии должна оканчиваться
    немедленной ошибкой. Этот хук можно вызывать из system_locks на старте.
    """
    raise ExchangeDirectionForbidden("Обратная конверсия EFHC→kWh запрещена каноном")


# -----------------------------------------------------------------------------
# Дополнительные утилиты для UI/роутов
# -----------------------------------------------------------------------------

async def max_exchangeable_kwh(db: AsyncSession, *, user_id: int) -> Decimal:
    """
    Возвращает максимум, который можно обменять прямо сейчас (равно available_kwh).
    """
    snap = await _user_energy_snapshot(db, user_id)
    return d8(snap["available_kwh"])


async def user_balances_brief(db: AsyncSession, *, user_id: int) -> Dict[str, Decimal]:
    """
    Короткая сводка по балансу, чтобы роуты могли быстро отрисовывать экран обмена.
    """
    row = await db.execute(
        text(
            f"""
            SELECT COALESCE(main_balance,0)          AS main_balance,
                   COALESCE(bonus_balance,0)         AS bonus_balance,
                   COALESCE(available_kwh,0)         AS available_kwh,
                   COALESCE(total_generated_kwh,0)   AS total_generated_kwh
              FROM {SCHEMA}.users
             WHERE id = :id
            """
        ),
        {"id": int(user_id)},
    )
    r = row.fetchone()
    if not r:
        raise ExchangeValidationError("Пользователь не найден")
    return {
        "main_balance": d8(r[0]),
        "bonus_balance": d8(r[1]),
        "available_kwh": d8(r[2]),
        "total_generated_kwh": d8(r[3]),
    }


# -----------------------------------------------------------------------------
# ИИ-поддержка/самодиагностика (лёгкая)
# -----------------------------------------------------------------------------

async def health_snapshot(db: AsyncSession) -> Dict[str, Any]:
    """
    Лёгкий «health» обменника:
      • пользователи с нулевым available_kwh (для статистики UI),
      • суммарная доступная энергия в системе (оценка «свободной энергии»),
      • метка канонического курса.
    Не тяжёлый запрос: считает агрегаты по users.
    """
    total_energy_row = await db.execute(
        text(f"SELECT COALESCE(SUM(available_kwh),0) FROM {SCHEMA}.users")
    )
    total_energy = d8(total_energy_row.scalar() or 0)

    zero_users_row = await db.execute(
        text(f"SELECT COUNT(*) FROM {SCHEMA}.users WHERE COALESCE(available_kwh,0) = 0")
    )
    zero_users = int(zero_users_row.scalar() or 0)

    return {
        "total_available_kwh_system": total_energy,
        "users_with_zero_available": zero_users,
        "rate_kwh_to_efhc": KWH_TO_EFHC_RATE,
    }


# =============================================================================
# Примеры использования (для разработчика)
# -----------------------------------------------------------------------------
# from backend.app.services import exchange_service as ex
#
# async def api_exchange(db, user_id: int, amount: str, idem: str):
#     """
#     Пример ручки/интеракции:
#       • берём amount из запроса пользователя (строкой), idem — с клиента
#       • пробуем выполнить обмен
#     """
#     try:
#         res = await ex.request_exchange(
#             db,
#             user_id=user_id,
#             amount_kwh=amount,
#             idempotency_key=idem,
#         )
#         # success JSON:
#         return {
#             "ok": True,
#             "exchanged_kwh": str(res.exchanged_kwh),
#             "credited_efhc": str(res.credited_efhc),
#             "new_main_balance": str(res.new_main_balance),
#             "new_available_kwh": str(res.new_available_kwh),
#             "idempotency_key": res.idempotency_key,
#         }
#     except ExchangeValidationError as ve:
#         return {"ok": False, "error": "validation", "detail": str(ve)}
#     except ExchangeInsufficientEnergy as ie:
#         return {"ok": False, "error": "insufficient_energy", "detail": str(ie)}
#     except ExchangeDirectionForbidden as df:
#         return {"ok": False, "error": "forbidden", "detail": str(df)}
#     except ExchangeError as ee:
#         return {"ok": False, "error": "exchange_error", "detail": str(ee)}
#     except Exception as e:
#         # Непредвиденное — логируем и отдаём общий ответ
#         logger.exception("api_exchange unexpected: %s", e)
#         return {"ok": False, "error": "internal", "detail": "Временная ошибка, повторите попытку."}
# =============================================================================

__all__ = [
    "ExchangeError",
    "ExchangeValidationError",
    "ExchangeDirectionForbidden",
    "ExchangeInsufficientEnergy",
    "ExchangeIdempotencyRequired",
    "ExchangePreview",
    "ExchangeResult",
    "preview_exchange",
    "request_exchange",
    "max_exchangeable_kwh",
    "user_balances_brief",
    "forbid_reverse_exchange",
    "health_snapshot",
]
