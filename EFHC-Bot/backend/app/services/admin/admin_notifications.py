# -*- coding: utf-8 -*-
# backend/app/services/admin/admin_notifications.py
# =============================================================================
# EFHC Bot — Уведомления админам (внутренние события + Telegram)
# -----------------------------------------------------------------------------
# Назначение:
#   • Централизованная работа с административными уведомлениями:
#       - запись событий в таблицу admin_notifications;
#       - (опционально) отправка уведомлений в Telegram-чат админов;
#       - безопасное чтение списка уведомлений для админ-панели.
#
# Инварианты и ИИ-защита:
#   1) НИКАКИХ денежных операций — только логика уведомлений.
#   2) Отсутствие/ошибка Telegram-отправки НИКОГДА не ломает бизнес-логику:
#        • запись в БД остаётся источником истины;
#        • ошибки сети/токена тихо логируются и игнорируются.
#   3) Все публичные методы устойчивы к некорректным параметрам
#      (мягкая валидация, понятные исключения).
#   4) Таблица {SCHEMA_ADMIN}.admin_notifications — единая точка хранения
#      всех админ-событий (audit-лог уведомлений).
# =============================================================================

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, validator

from sqlalchemy import text
from sqlalchemy.engine import Result
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config_core import get_settings
from backend.app.core.logging_core import get_logger

logger = get_logger(__name__)
S = get_settings()

SCHEMA_ADMIN: str = getattr(S, "DB_SCHEMA_ADMIN", "efhc_admin") or "efhc_admin"

# Опциональная зависимость: httpx для отправки в Telegram.
# Если не установлена — отправка в TG будет тихо отключена, запись в БД сохранится.
try:  # pragma: no cover - зависимость опциональна
    import httpx  # type: ignore
except Exception:  # pragma: no cover
    httpx = None  # type: ignore[assignment]

# Настройки Telegram-канала для уведомлений
ADMIN_NOTIFY_CHAT_ID: str = str(getattr(S, "ADMIN_NOTIFICATIONS_CHAT_ID", "") or "")
TELEGRAM_BOT_TOKEN: str = str(getattr(S, "TELEGRAM_BOT_TOKEN", "") or "")


# =============================================================================
# DTO-модели
# =============================================================================

class AdminNotification(BaseModel):
    """
    Одна запись уведомления для отображения в админ-панели.
    """
    id: int
    event: str = Field(..., description="Короткий код события (NEW_WITHDRAWAL, LOTTERY_WINNER и т.п.)")
    payload_json: str = Field(..., description="Сырое JSON-описание полезной нагрузки")
    status: str = Field(..., description="Статус обработки (NEW / SENT / ERROR / IGNORED / ...)")
    created_at: str = Field(..., description="Момент создания (ISO-строка)")

    @validator("created_at", pre=True)
    def _norm_created_at(cls, v: Any) -> str:
        if isinstance(v, datetime):
            return v.isoformat()
        return str(v)


class NotificationsFilter(BaseModel):
    """
    Фильтр для списка уведомлений.

    Важно: лимиты достаточно жёсткие, чтобы не перегружать БД и интерфейс.
    """
    status: Optional[str] = Field(
        default=None,
        description="Фильтр по статусу (например, 'NEW', 'ERROR'). Если None — все статусы.",
    )
    limit: int = Field(50, ge=1, le=500)
    offset: int = Field(0, ge=0)
    sort_desc: bool = Field(True, description="True — новые сверху")


# =============================================================================
# Внутренние утилиты
# =============================================================================

async def _store_notification(
    db: AsyncSession,
    *,
    event: str,
    payload_json: str,
    status: str = "NEW",
) -> int:
    """
    Записывает уведомление в таблицу admin_notifications и возвращает его id.

    ИИ-защита:
      • Любые ошибки INSERT логируются и прокидываются выше — админ-сервис
        решает, что делать (обычно транзакция и так откатится).
    """
    r: Result = await db.execute(
        text(
            f"""
            INSERT INTO {SCHEMA_ADMIN}.admin_notifications
                (event, payload_json, status, created_at)
            VALUES
                (:e, :p, :s, NOW() AT TIME ZONE 'UTC')
            RETURNING id
            """
        ),
        {"e": event, "p": payload_json, "s": status},
    )
    notif_id = int(r.scalar_one())
    return notif_id


async def _send_telegram_message(text_message: str) -> None:
    """
    Асинхронная отправка сообщения в Telegram-чат админов.

    ИИ-защита:
      • Если TELEGRAM_BOT_TOKEN или ADMIN_NOTIFY_CHAT_ID не заданы — тихо выходим.
      • Если httpx отсутствует — отправка пропускается (но не ломает систему).
      • Любые сетевые ошибки логируются как warning, но не поднимаются наружу.
    """
    if not TELEGRAM_BOT_TOKEN or not ADMIN_NOTIFY_CHAT_ID:
        # Telegram-уведомления не сконфигурированы — просто выходим.
        return

    if httpx is None:  # type: ignore[truthy-function]
        logger.debug("Admin notifications: httpx не установлен, Telegram-отправка отключена")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": ADMIN_NOTIFY_CHAT_ID,
        "text": text_message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:  # type: ignore[attr-defined]
            await client.post(url, json=payload)
    except Exception as e:  # pragma: no cover - сетевой слой
        # Не роняем бизнес-логику, просто логируем
        logger.warning("Admin notifications: ошибка отправки в Telegram: %s", e)


# =============================================================================
# AdminNotifier — высокоуровневый сервис уведомлений
# =============================================================================

class AdminNotifier:
    """
    Централизованный сервис уведомлений, который:
      • записывает событие в таблицу admin_notifications;
      • (опционально) отправляет текст в Telegram.

    Концепция:
      • Все публичные методы NOTIFY_* — это семантические обёртки вокруг
        базового notify_generic(...), чтобы фронтенд/роуты могли вызывать
        их по понятным именам без ручной сборки JSON.
    """

    # -------------------------------------------------------------------------
    # БАЗОВЫЙ УНИВЕРСАЛЬНЫЙ МЕТОД
    # -------------------------------------------------------------------------

    @staticmethod
    async def notify_generic(
        db: AsyncSession,
        *,
        event: str,
        message: str,
        payload_json: str = "{}",
        send_telegram: bool = True,
    ) -> int:
        """
        Универсальное уведомление.

        Параметры:
          • event        — короткое имя события (например, 'BANK_MINT'),
          • message      — человекочитаемое описание (для Telegram),
          • payload_json — сырое JSON-тело для хранения,
          • send_telegram — флаг, нужно ли пытаться отправлять в Telegram.

        Возвращает:
          • ID записи уведомления в БД (admin_notifications.id).

        ИИ-защита:
          • Любые ошибки при записи в БД НЕ скрываются — пусть вызывающий код
            решает, откатывать ли транзакцию.
          • Ошибки Telegram-отправки логируются, но не прерывают выполнение.
        """
        if not event or not isinstance(event, str):
            raise ValueError("event должен быть непустой строкой")

        if not payload_json:
            payload_json = "{}"

        notif_id = await _store_notification(
            db,
            event=event,
            payload_json=payload_json,
            status="NEW",
        )

        # Отправка в Telegram — best effort, без влияния на БД.
        if send_telegram:
            await _send_telegram_message(f"🔔 {event}\n{message}\nNID: {notif_id}")

        return notif_id

    # -------------------------------------------------------------------------
    # СПЕЦИАЛИЗИРОВАННЫЕ СОБЫТИЯ (ДЛЯ ДРУГИХ СЕРВИСОВ)
    # -------------------------------------------------------------------------

    @staticmethod
    async def notify_new_withdrawal(
        db: AsyncSession,
        *,
        request_id: int,
        user_id: int,
        amount_efhc: str,
    ) -> int:
        """
        Уведомление о новой заявке на вывод EFHC.

        Используется в admin_withdrawals_service при создании заявки:
          • event        = 'NEW_WITHDRAWAL'
          • payload_json = {"request_id": ..., "user_id": ..., "amount": "..."}
        """
        payload = (
            f'{{"request_id":{int(request_id)},'
            f'"user_id":{int(user_id)},'
            f'"amount":"{amount_efhc}"}}'
        )
        message = (
            f"💸 Новая заявка на вывод #{int(request_id)}\n"
            f"Пользователь: <code>{int(user_id)}</code>\n"
            f"Сумма: <b>{amount_efhc} EFHC</b>"
        )
        return await AdminNotifier.notify_generic(
            db,
            event="NEW_WITHDRAWAL",
            message=message,
            payload_json=payload,
            send_telegram=True,
        )

    @staticmethod
    async def notify_ref_level(
        db: AsyncSession,
        *,
        referrer_id: int,
        threshold: int,
        bonus_efhc: str,
    ) -> int:
        """
        Уведомление о достижении реферального порога (10/100/1000/...).

        Используется в admin_referral_service:
          • event        = 'REFERRAL_LEVEL'
          • payload_json = {"referrer_id": ..., "threshold": ..., "bonus": "..."}
        """
        payload = (
            f'{{"referrer_id":{int(referrer_id)},'
            f'"threshold":{int(threshold)},'
            f'"bonus":"{bonus_efhc}"}}'
        )
        message = (
            f"👥 Достигнут реф-уровень {int(threshold)}\n"
            f"Реферер: <code>{int(referrer_id)}</code>\n"
            f"Бонус: <b>{bonus_efhc} EFHC</b>"
        )
        return await AdminNotifier.notify_generic(
            db,
            event="REFERRAL_LEVEL",
            message=message,
            payload_json=payload,
            send_telegram=True,
        )

    @staticmethod
    async def notify_lottery_winner(
        db: AsyncSession,
        *,
        lottery_id: int,
        user_id: int,
        prize: str,
        title: Optional[str] = None,
    ) -> int:
        """
        Уведомление о победителе лотереи (один победитель на розыгрыш).

        Используется в admin_lotteries_service:
          • event        = 'LOTTERY_WINNER'
          • payload_json = {"lottery_id": ..., "user_id": ..., "prize": "...", "title": "..."}
        """
        title_safe = (title or "").replace('"', '\\"')
        payload = (
            f'{{"lottery_id":{int(lottery_id)},'
            f'"user_id":{int(user_id)},'
            f'"prize":"{prize}",'
            f'"title":"{title_safe}"}}'
        )
        cap_title = f" «{title}»" if title else ""
        message = (
            f"🎉 Победитель лотереи #{int(lottery_id)}{cap_title}\n"
            f"Пользователь: <code>{int(user_id)}</code>\n"
            f"Приз: <b>{prize}</b>"
        )
        return await AdminNotifier.notify_generic(
            db,
            event="LOTTERY_WINNER",
            message=message,
            payload_json=payload,
            send_telegram=True,
        )

    @staticmethod
    async def notify_bank_mint(
        db: AsyncSession,
        *,
        amount_efhc: str,
    ) -> int:
        """
        Уведомление о минте EFHC банком (BANK_MINT).

        Используется в admin_bank_service.mint_efhc.
        """
        payload = f'{{"amount":"{amount_efhc}"}}'
        message = f"🏦 Минт EFHC банком: <b>{amount_efhc} EFHC</b>"
        return await AdminNotifier.notify_generic(
            db,
            event="BANK_MINT",
            message=message,
            payload_json=payload,
            send_telegram=True,
        )

    @staticmethod
    async def notify_bank_burn(
        db: AsyncSession,
        *,
        amount_efhc: str,
    ) -> int:
        """
        Уведомление о сжигании EFHC банком (BANK_BURN).

        Используется в admin_bank_service.burn_efhc.
        """
        payload = f'{{"amount":"{amount_efhc}"}}'
        message = f"🔥 Сжигание EFHC банком: <b>{amount_efhc} EFHC</b>"
        return await AdminNotifier.notify_generic(
            db,
            event="BANK_BURN",
            message=message,
            payload_json=payload,
            send_telegram=True,
        )

    # При необходимости сюда можно добавлять новые семантические обёртки:
    #   • notify_panel_created(...)
    #   • notify_panel_deactivated(...)
    #   • notify_vip_granted(...)
    #   и т.п., сохраняя общий стиль.


# =============================================================================
# Функции чтения уведомлений для админ-панели
# =============================================================================

class AdminNotificationsService:
    """
    Чтение и простое управление уведомлениями для UI админ-панели.
    """

    @staticmethod
    async def list_notifications(
        db: AsyncSession,
        flt: Optional[NotificationsFilter] = None,
    ) -> List[AdminNotification]:
        """
        Возвращает список уведомлений с возможностью фильтрации по статусу.

        Параметры:
          • flt.status   — если задан, фильтрует по status;
          • flt.limit    — количество записей (1..500);
          • flt.offset   — смещение;
          • flt.sort_desc — порядок сортировки по id (DESC/ASC).
        """
        flt = flt or NotificationsFilter()

        where = ["1=1"]
        params: Dict[str, Any] = {
            "limit": flt.limit,
            "offset": flt.offset,
        }
        if flt.status:
            where.append("status = :st")
            params["st"] = flt.status

        order = "DESC" if flt.sort_desc else "ASC"

        r: Result = await db.execute(
            text(
                f"""
                SELECT id, event, payload_json, status, created_at
                FROM {SCHEMA_ADMIN}.admin_notifications
                WHERE {" AND ".join(where)}
                ORDER BY id {order}
                LIMIT :limit OFFSET :offset
                """
            ),
            params,
        )

        out: List[AdminNotification] = []
        for row in r.fetchall():
            out.append(
                AdminNotification(
                    id=int(row.id),
                    event=str(row.event),
                    payload_json=str(row.payload_json),
                    status=str(row.status),
                    created_at=row.created_at,
                )
            )
        return out

    @staticmethod
    async def mark_notification_status(
        db: AsyncSession,
        *,
        notification_id: int,
        status: str,
    ) -> None:
        """
        Обновляет статус уведомления (например, NEW → SEEN).

        ИИ-защита:
          • Статус не нормализуем жёстко, но ожидается небольшой фиксированный набор:
              NEW / SEEN / IGNORED / ERROR / SENT / ...
        """
        if notification_id <= 0:
            raise ValueError("notification_id должен быть > 0")
        if not status:
            raise ValueError("status должен быть непустой строкой")

        await db.execute(
            text(
                f"""
                UPDATE {SCHEMA_ADMIN}.admin_notifications
                SET status = :st
                WHERE id = :nid
                """
            ),
            {"st": status, "nid": int(notification_id)},
        )


__all__ = [
    "AdminNotification",
    "NotificationsFilter",
    "AdminNotifier",
    "AdminNotificationsService",
]

