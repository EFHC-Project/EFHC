# -*- coding: utf-8 -*-
# backend/app/services/admin/admin_logging.py
# =============================================================================
# EFHC Bot — Логирование действий администраторов
# -----------------------------------------------------------------------------
# Назначение модуля:
#   • Централизованный сервис логов админ-панели.
#   • Фиксация всех важных действий админов (минт/бёрн, выдача призов, настройки,
#     ручные корректировки и т.д.) в таблице admin_logs.
#   • Предоставление удобной выборки логов для UI админ-панели.
#
# ИИ-защита / инварианты:
#   • Ошибка записи лога НИКОГДА не должна ломать бизнес-операцию:
#       - write() логирует сбой через get_logger и молча возвращает управление.
#   • Все SQL-запросы только с bind-параметрами (никакого конкатенированного SQL).
#   • Любые неожиданные поля/типы аккуратно приводятся к строкам/числам.
#   • Параметры выборки (limit/offset) жёстко ограничиваются безопасными границами.
#
# Ожидаемая схема таблицы (канон):
#   {SCHEMA_ADMIN}.admin_logs (
#       id         BIGSERIAL PRIMARY KEY,
#       admin_id   BIGINT NOT NULL,
#       action     TEXT   NOT NULL,
#       entity     TEXT   NOT NULL,
#       entity_id  BIGINT NULL,
#       timestamp  TIMESTAMPTZ NOT NULL DEFAULT NOW() AT TIME ZONE 'UTC',
#       details    TEXT NULL
#   );
#
# Как использовать:
#   • Для записи:
#       await AdminLogger.write(db, admin_id=admin.id, action="MINT",
#                               entity="bank", entity_id=None, details="amount=100")
#
#   • Для выборки (в админке):
#       logs = await AdminLogger.list_logs(db, limit=100, offset=0,
#                                          admin_id=..., action="MINT")
# =============================================================================

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from sqlalchemy import text
from sqlalchemy.engine import Result
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config_core import get_settings
from backend.app.core.logging_core import get_logger

logger = get_logger(__name__)
S = get_settings()

SCHEMA_ADMIN: str = getattr(S, "DB_SCHEMA_ADMIN", "efhc_admin") or "efhc_admin"


# =============================================================================
# DTO для логов
# =============================================================================

class AdminLog(BaseModel):
    """
    Одна запись в журнале действий админов.

    Поля:
      • id        — внутренний идентификатор записи;
      • admin_id  — ID администратора (из admin_users.id);
      • action    — короткий код действия (MINT, BURN, SET_SETTING, DRAW и т.д.);
      • entity    — над чем выполнялось действие (bank, lottery, panel, user_balance…);
      • entity_id — опциональный ID сущности (может быть NULL);
      • timestamp — ISO8601-строка (UTC) времени события;
      • details   — произвольная строка с деталями (JSON/ключ=значение и т.п.).
    """
    id: int = Field(...)
    admin_id: int = Field(...)
    action: str = Field(..., min_length=1, max_length=128)
    entity: str = Field(..., min_length=1, max_length=128)
    entity_id: Optional[int] = None
    timestamp: str = Field(...)
    details: Optional[str] = None


# =============================================================================
# Сервис логов
# =============================================================================

class AdminLogger:
    """
    Сервис логирования действий администраторов.

    Ключевые принципы:
      • Логирование — вспомогательная функция. Ошибка логирования не должна
        останавливать минт/бёрн/выдачу призов и т.д.
      • Все параметры приводятся к безопасным строкам и числам.
      • Поддерживается базовая фильтрация при выборке логов.
    """

    # -------------------------------------------------------------------------
    # Запись лога
    # -------------------------------------------------------------------------

    @staticmethod
    async def write(
        db: AsyncSession,
        *,
        admin_id: int,
        action: str,
        entity: str,
        entity_id: Optional[int] = None,
        details: Optional[str] = None,
    ) -> None:
        """
        Пишет строку в admin_logs.

        ИИ-защита:
          • Любые исключения внутри этой функции перехватываются и логируются
            через logger.error, но НЕ пробрасываются выше — бизнес-логика
            продолжает выполняться.
          • Ограничение длины строк action/entity/details — на уровне кода
            для уменьшения риска переполнения полей.
        """
        try:
            if admin_id <= 0:
                # Некорректный admin_id — не пишем лог, но не считаем это фатальной ошибкой.
                logger.warning("AdminLogger.write: некорректный admin_id=%s", admin_id)
                return

            action_s = str(action or "").strip()[:128]
            entity_s = str(entity or "").strip()[:128]
            details_s: Optional[str] = None
            if details is not None:
                # Жёстко режем до разумного лимита (например, 4000 символов).
                details_s = str(details)[:4000]

            if not action_s or not entity_s:
                logger.warning(
                    "AdminLogger.write: пустой action/entity (admin_id=%s, raw_action=%r, raw_entity=%r)",
                    admin_id, action, entity,
                )
                return

            sql = text(
                f"""
                INSERT INTO {SCHEMA_ADMIN}.admin_logs
                    (admin_id, action, entity, entity_id, timestamp, details)
                VALUES
                    (:admin_id, :action, :entity, :entity_id, NOW() AT TIME ZONE 'UTC', :details)
                """
            )
            await db.execute(
                sql,
                {
                    "admin_id": int(admin_id),
                    "action": action_s,
                    "entity": entity_s,
                    "entity_id": int(entity_id) if entity_id is not None else None,
                    "details": details_s,
                },
            )
        except Exception as e:  # noqa: BLE001
            # НИКОГДА не роняем бизнес-операции из-за логов
            logger.error(
                "AdminLogger.write: failed to insert log (admin_id=%s, action=%r, entity=%r): %s",
                admin_id,
                action,
                entity,
                e,
            )

    # -------------------------------------------------------------------------
    # Выборка логов для UI
    # -------------------------------------------------------------------------

    @staticmethod
    async def list_logs(
        db: AsyncSession,
        *,
        limit: int = 50,
        offset: int = 0,
        sort_desc: bool = True,
        admin_id: Optional[int] = None,
        action: Optional[str] = None,
        entity: Optional[str] = None,
    ) -> List[AdminLog]:
        """
        Возвращает список логов с базовыми фильтрами.

        Параметры:
          • limit      — максимум записей (1..500);
          • offset     — смещение (>=0);
          • sort_desc  — сортировка по id: DESC (по умолчанию) или ASC;
          • admin_id   — фильтрация по администратору;
          • action     — фильтрация по коду действия;
          • entity     — фильтрация по сущности.

        ИИ-защита:
          • limit и offset нормализуются в безопасные диапазоны.
          • В случае ошибки БД выбрасывается исключение (пусть роут решает,
            как отвечать клиенту), но ошибка при маппинге отдельных строк
            приводит лишь к пропуску этих строк, а не всего списка.
        """
        # Нормализация limit/offset
        if limit < 1:
            limit = 1
        if limit > 500:
            limit = 500
        if offset < 0:
            offset = 0

        where: List[str] = ["1=1"]
        params: Dict[str, Any] = {
            "limit": limit,
            "offset": offset,
        }

        if admin_id is not None:
            where.append("admin_id = :aid")
            params["aid"] = int(admin_id)

        if action:
            where.append("action = :act")
            params["act"] = str(action).strip()[:128]

        if entity:
            where.append("entity = :ent")
            params["ent"] = str(entity).strip()[:128]

        order = "DESC" if sort_desc else "ASC"

        sql = text(
            f"""
            SELECT id, admin_id, action, entity, entity_id, timestamp, details
            FROM {SCHEMA_ADMIN}.admin_logs
            WHERE {" AND ".join(where)}
            ORDER BY id {order}
            LIMIT :limit OFFSET :offset
            """
        )

        r: Result = await db.execute(sql, params)
        rows = r.fetchall()

        out: List[AdminLog] = []
        for row in rows:
            try:
                out.append(
                    AdminLog(
                        id=int(getattr(row, "id")),
                        admin_id=int(getattr(row, "admin_id")),
                        action=str(getattr(row, "action")),
                        entity=str(getattr(row, "entity")),
                        entity_id=(
                            int(getattr(row, "entity_id"))
                            if getattr(row, "entity_id", None) is not None
                            else None
                        ),
                        timestamp=(
                            row.timestamp.isoformat()
                            if hasattr(row.timestamp, "isoformat")
                            else str(row.timestamp)
                        ),
                        details=(
                            str(getattr(row, "details"))
                            if getattr(row, "details", None) is not None
                            else None
                        ),
                    )
                )
            except Exception as e:  # noqa: BLE001
                # Если конкретная строка «битая», пишем в лог и пропускаем её
                logger.warning("AdminLogger.list_logs: skip broken row %r: %s", row, e)
                continue

        return out


__all__ = [
    "AdminLog",
    "AdminLogger",
]

