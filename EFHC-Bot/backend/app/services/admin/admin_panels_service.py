# -*- coding: utf-8 -*-
# backend/app/services/admin/admin_panels_service.py
# =============================================================================
# EFHC Bot — Панели пользователей (админ-управление)
# -----------------------------------------------------------------------------
# Назначение:
#   • Административное управление панелями пользователей:
#       - создание/активация панелей,
#       - деактивация панелей (по id или «последней активной»),
#       - просмотр панелей пользователя,
#       - health-сводка по панелям для ИИ-диагностики.
#
# Ключевые инварианты канона:
#   1) Никаких движений EFHC в этом модуле.
#      • Здесь мы трогаем ТОЛЬКО таблицу panels (и читаем users).
#      • Покупка панелей за EFHC осуществляется отдельными сервисами
#        через transactions_service (Банк ↔ Пользователь).
#   2) Максимум панелей на пользователя — MAX_PANELS_PER_USER (по канону 1000).
#      • Админ не может создать/активировать панель, если лимит достигнут.
#   3) Генерация энергии по панелям делается ТОЛЬКО через energy_service:
#      • last_generated_at / expires_at используются для идемпотентного
#        начисления kWh; здесь мы не вмешиваемся в генерацию.
#   4) Панель считается:
#      • активной, если is_active = TRUE и NOW() < expires_at;
#      • истёкшей, если NOW() >= expires_at (is_active может быть TRUE или FALSE).
#   5) Все критичные админ-действия логируются (AdminLogger) и,
#      при необходимости, сопровождаются уведомлениями (AdminNotifier).
#
# ИИ-защита:
#   • Проверка лимитов панелей.
#   • Защита от «бессмысленных» операций (повторная активация/деактивация).
#   • Health-диагностика: аномалии по last_generated_at/expires_at.
# =============================================================================

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, validator

from sqlalchemy import text
from sqlalchemy.engine import Result
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config_core import get_settings
from backend.app.core.logging_core import get_logger

from .admin_rbac import AdminUser, AdminRole, RBAC
from .admin_logging import AdminLogger
from .admin_notifications import AdminNotifier

logger = get_logger(__name__)
S = get_settings()

SCHEMA_CORE: str = getattr(S, "DB_SCHEMA_CORE", "efhc_core") or "efhc_core"

# Максимум панелей на пользователя (канон: 1000, но можно переопределить в .env)
MAX_PANELS_PER_USER: int = int(getattr(S, "MAX_PANELS_PER_USER", 1000) or 1000)

# Срок жизни панели по умолчанию (в днях) при ручной активации
PANEL_LIFESPAN_DAYS: int = int(getattr(S, "PANEL_LIFESPAN_DAYS", 365) or 365)


# =============================================================================
# Ошибки уровня сервиса (для дружелюбных сообщений в админке)
# =============================================================================

class PanelAdminError(Exception):
    """Базовая ошибка при админ-операциях с панелями."""


class PanelLimitExceeded(PanelAdminError):
    """Превышен лимит панелей на пользователя."""


class PanelNotFound(PanelAdminError):
    """Панель не найдена или не принадлежит пользователю."""


# =============================================================================
# DTO для админки
# =============================================================================

class PanelToggleRequest(BaseModel):
    """
    Запрос на включение/выключение панели пользователя.

    Сценарии:
      • activate=True, panel_id=None:
            → создать новую активную панель пользователю (если не достигнут лимит).
      • activate=False, panel_id=None:
            → деактивировать одну «последнюю» активную панель пользователя.
      • activate=False, panel_id != None:
            → деактивировать конкретную панель (если она принадлежит user_id).
      • activate=True, panel_id != None:
            → опционально можно реализовать повторную активацию (с контролем),
              но по умолчанию мы не предусматриваем «повторное включение»
              истёкших панелей (панель — фиксированный контракт).
    """
    user_id: int = Field(..., description="Внутренний ID пользователя (users.id)")
    panel_id: Optional[int] = Field(
        default=None,
        description="ID панели (если нужно выключить конкретную)",
    )
    activate: bool = Field(True, description="True — создать/включить, False — выключить")
    lifespan_days: Optional[int] = Field(
        default=None,
        ge=1,
        le=3650,
        description="Необязательный срок жизни новой панели (по умолчанию PANEL_LIFESPAN_DAYS)",
    )

    @validator("user_id")
    def _v_uid(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("user_id должен быть > 0")
        return v

    @validator("panel_id")
    def _v_pid(cls, v: Optional[int]) -> Optional[int]:
        if v is not None and v <= 0:
            raise ValueError("panel_id должен быть > 0")
        return v


class PanelBrief(BaseModel):
    """Краткое описание панели для списка в админке."""
    id: int
    user_id: int
    is_active: bool
    activated_at: Optional[str]
    expires_at: Optional[str]
    deactivated_at: Optional[str]
    last_generated_at: Optional[str]
    generated_kwh: str


class PanelsHealthSnapshot(BaseModel):
    """
    Health-сводка по панелям:
      • общее количество,
      • активные/истёкшие,
      • аномалии (для ИИ-диагностики).
    """
    total_panels: int
    active_panels: int
    expired_panels: int
    anomalous_last_gt_expiry: int
    anomalous_last_in_future: int
    anomalous_expired_but_active: int


# =============================================================================
# Вспомогательные функции
# =============================================================================

def _now_utc() -> datetime:
    return datetime.now(tz=timezone.utc)


async def _count_user_panels(db: AsyncSession, user_id: int) -> int:
    """Возвращает количество панелей пользователя (любых)."""
    r: Result = await db.execute(
        text(
            f"""
            SELECT COUNT(1) AS cnt
            FROM {SCHEMA_CORE}.panels
            WHERE user_id = :uid
            """
        ),
        {"uid": int(user_id)},
    )
    row = r.fetchone()
    return int(row.cnt or 0) if row else 0


async def _count_user_active_panels(db: AsyncSession, user_id: int) -> int:
    """Возвращает количество активных панелей пользователя."""
    r: Result = await db.execute(
        text(
            f"""
            SELECT COUNT(1) AS cnt
            FROM {SCHEMA_CORE}.panels
            WHERE user_id = :uid
              AND is_active = TRUE
            """
        ),
        {"uid": int(user_id)},
    )
    row = r.fetchone()
    return int(row.cnt or 0) if row else 0


async def _ensure_user_exists(db: AsyncSession, user_id: int) -> None:
    """Проверка существования пользователя (для дружелюбной ошибки)."""
    r: Result = await db.execute(
        text(
            f"""
            SELECT 1
            FROM {SCHEMA_CORE}.users
            WHERE id = :uid
            LIMIT 1
            """
        ),
        {"uid": int(user_id)},
    )
    if not r.fetchone():
        raise PanelAdminError("Пользователь не найден")


# =============================================================================
# AdminPanelsService — операции над панелями
# =============================================================================

class AdminPanelsService:
    """
    Сервис для админ-управления панелями:

      • toggle_panel(...) — включение/выключение панелей (с лимитами и логами);
      • list_user_panels(...) — просмотр панелей пользователя;
      • panels_health_snapshot(...) — ИИ-диагностика аномалий таблицы panels.

    Важно:
      • Модуль НЕ выполняет операций EFHC.
      • Все изменения панелей логируются (AdminLogger).
    """

    # -------------------------------------------------------------------------
    # ВКЛЮЧЕНИЕ / ВЫКЛЮЧЕНИЕ ПАНЕЛЕЙ
    # -------------------------------------------------------------------------

    @staticmethod
    async def toggle_panel(
        db: AsyncSession,
        req: PanelToggleRequest,
        admin: AdminUser,
    ) -> Dict[str, Any]:
        """
        Включает (создаёт активную панель) или выключает (деактивирует) панель пользователя.

        ИИ-защита:
          • Контроль существования пользователя.
          • Контроль лимита MAX_PANELS_PER_USER при создании.
          • Безопасная деактивация (только панелей пользователя).
        """
        RBAC.require_role(admin, AdminRole.MODERATOR)
        await _ensure_user_exists(db, req.user_id)

        if req.activate:
            # Создание новой панели
            return await AdminPanelsService._create_new_panel(db, req, admin)
        else:
            # Деактивация
            return await AdminPanelsService._deactivate_panel(db, req, admin)

    @staticmethod
    async def _create_new_panel(
        db: AsyncSession,
        req: PanelToggleRequest,
        admin: AdminUser,
    ) -> Dict[str, Any]:
        """
        Создаёт новую активную панель пользователю.
        Не влияет на EFHC-баланс (покупка панелей — отдельный процесс через Банк).
        """
        # Проверка лимитов по общему количеству панелей (не только активных)
        total_panels = await _count_user_panels(db, req.user_id)
        if total_panels >= MAX_PANELS_PER_USER:
            raise PanelLimitExceeded(
                f"Превышен лимит панелей ({MAX_PANELS_PER_USER}) для пользователя {req.user_id}"
            )

        lifespan_days = req.lifespan_days or PANEL_LIFESPAN_DAYS
        now_ts = _now_utc()
        expires_at = now_ts + timedelta(days=lifespan_days)

        # ВАЖНО: поля last_generated_at и generated_kwh должны быть инициализированы
        # так, чтобы energy_service мог корректно «догонять» генерацию.
        # Типичная схема:
        #   last_generated_at = activated_at
        #   generated_kwh = 0
        r: Result = await db.execute(
            text(
                f"""
                INSERT INTO {SCHEMA_CORE}.panels
                    (user_id,
                     is_active,
                     activated_at,
                     expires_at,
                     last_generated_at,
                     generated_kwh)
                VALUES
                    (:uid,
                     TRUE,
                     :act,
                     :exp,
                     :last_gen,
                     0)
                RETURNING id
                """
            ),
            {
                "uid": req.user_id,
                "act": now_ts,
                "exp": expires_at,
                "last_gen": now_ts,
            },
        )
        row = r.fetchone()
        if not row:
            raise PanelAdminError("Не удалось создать панель (INSERT не вернул id)")
        panel_id = int(row.id)

        await AdminLogger.write(
            db,
            admin_id=admin.id,
            action="PANEL_ON",
            entity="panel",
            entity_id=panel_id,
            details=f"user_id={req.user_id}; lifespan_days={lifespan_days}",
        )

        # Уведомление (опционально, без критичности)
        await AdminNotifier.notify_generic(
            db,
            event="PANEL_CREATED",
            message=f"Создана новая панель #{panel_id} для пользователя {req.user_id}",
            payload_json=(
                f'{{"panel_id":{panel_id},"user_id":{req.user_id},'
                f'"lifespan_days":{lifespan_days}}}'
            ),
        )

        return {
            "ok": True,
            "created_panel_id": panel_id,
            "user_id": req.user_id,
            "expires_at": expires_at.isoformat(),
        }

    @staticmethod
    async def _deactivate_panel(
        db: AsyncSession,
        req: PanelToggleRequest,
        admin: AdminUser,
    ) -> Dict[str, Any]:
        """
        Деактивирует панель:
          • если указан panel_id — эту панель (если принадлежит user_id);
          • если panel_id не указан — одну последнюю активную панель.
        """
        now_ts = _now_utc()

        if req.panel_id:
            # Деактивируем конкретную панель
            r: Result = await db.execute(
                text(
                    f"""
                    UPDATE {SCHEMA_CORE}.panels
                    SET is_active = FALSE,
                        deactivated_at = :now
                    WHERE id = :pid
                      AND user_id = :uid
                      AND is_active = TRUE
                    RETURNING id
                    """
                ),
                {"pid": req.panel_id, "uid": req.user_id, "now": now_ts},
            )
            row = r.fetchone()
            if not row:
                # Возможно панель уже выключена или не принадлежит пользователю
                raise PanelNotFound(
                    f"Активная панель #{req.panel_id} пользователя {req.user_id} не найдена"
                )
            panel_id = int(row.id)
        else:
            # Деактивируем «последнюю» активную панель (по id DESC)
            r: Result = await db.execute(
                text(
                    f"""
                    UPDATE {SCHEMA_CORE}.panels
                    SET is_active = FALSE,
                        deactivated_at = :now
                    WHERE id = (
                        SELECT id
                        FROM {SCHEMA_CORE}.panels
                        WHERE user_id = :uid
                          AND is_active = TRUE
                        ORDER BY id DESC
                        LIMIT 1
                    )
                    RETURNING id
                    """
                ),
                {"uid": req.user_id, "now": now_ts},
            )
            row = r.fetchone()
            if not row:
                raise PanelNotFound(
                    f"У пользователя {req.user_id} нет активных панелей для деактивации"
                )
            panel_id = int(row.id)

        await AdminLogger.write(
            db,
            admin_id=admin.id,
            action="PANEL_OFF",
            entity="panel",
            entity_id=panel_id,
            details=f"user_id={req.user_id}",
        )

        await AdminNotifier.notify_generic(
            db,
            event="PANEL_DEACTIVATED",
            message=f"Панель #{panel_id} пользователя {req.user_id} деактивирована администратором",
            payload_json=f'{{"panel_id":{panel_id},"user_id":{req.user_id}}}',
        )

        return {
            "ok": True,
            "deactivated_panel_id": panel_id,
            "user_id": req.user_id,
        }

    # -------------------------------------------------------------------------
    # ПРОСМОТР ПАНЕЛЕЙ ПОЛЬЗОВАТЕЛЯ
    # -------------------------------------------------------------------------

    @staticmethod
    async def list_user_panels(
        db: AsyncSession,
        *,
        user_id: int,
        include_expired: bool = True,
    ) -> List[PanelBrief]:
        """
        Возвращает список панелей пользователя для админки.

        Параметры:
          • user_id — внутренний ID пользователя.
          • include_expired — если False, возвращаем только активные панели.
        """
        await _ensure_user_exists(db, user_id)

        where = ["user_id = :uid"]
        params: Dict[str, Any] = {"uid": int(user_id)}

        if not include_expired:
            where.append("is_active = TRUE")

        sql = text(
            f"""
            SELECT
                id,
                user_id,
                is_active,
                activated_at,
                expires_at,
                deactivated_at,
                last_generated_at,
                COALESCE(generated_kwh, 0) AS generated_kwh
            FROM {SCHEMA_CORE}.panels
            WHERE {" AND ".join(where)}
            ORDER BY id DESC
            """
        )
        r: Result = await db.execute(sql, params)

        out: List[PanelBrief] = []
        for row in r.fetchall():
            out.append(
                PanelBrief(
                    id=int(row.id),
                    user_id=int(row.user_id),
                    is_active=bool(row.is_active),
                    activated_at=row.activated_at.isoformat() if getattr(row, "activated_at", None) and hasattr(row.activated_at, "isoformat") else None,
                    expires_at=row.expires_at.isoformat() if getattr(row, "expires_at", None) and hasattr(row.expires_at, "isoformat") else None,
                    deactivated_at=row.deactivated_at.isoformat() if getattr(row, "deactivated_at", None) and hasattr(row.deactivated_at, "isoformat") else None,
                    last_generated_at=row.last_generated_at.isoformat() if getattr(row, "last_generated_at", None) and hasattr(row.last_generated_at, "isoformat") else None,
                    generated_kwh=str(row.generated_kwh),
                )
            )
        return out

    # -------------------------------------------------------------------------
    # HEALTH-ДИАГНОСТИКА ПАНЕЛЕЙ (ИИ-поддержка)
    # -------------------------------------------------------------------------

    @staticmethod
    async def panels_health_snapshot(db: AsyncSession) -> PanelsHealthSnapshot:
        """
        Лёгкая health-диагностика таблицы panels:

          • total_panels          — всего панелей в системе;
          • active_panels         — is_active = TRUE;
          • expired_panels        — is_active = FALSE или expires_at < NOW();
          • anomalous_last_gt_expiry
               — количество панелей, у которых last_generated_at > expires_at;
          • anomalous_last_in_future
               — количество панелей, где last_generated_at > NOW();
          • anomalous_expired_but_active
               — количество панелей, у которых is_active = TRUE,
                 но expires_at < NOW() (логическая аномалия).

        Эта функция не меняет данные, только читает; пригодна для периодического
        мониторинга и отображения в админ-дэшборде.
        """
        now_ts = _now_utc()

        # Общая статистика
        r1: Result = await db.execute(
            text(
                f"""
                SELECT
                    COUNT(1) AS total_panels,
                    SUM(CASE WHEN is_active THEN 1 ELSE 0 END) AS active_panels
                FROM {SCHEMA_CORE}.panels
                """
            )
        )
        s1 = r1.fetchone() or {}
        total_panels = int(getattr(s1, "total_panels", 0) or 0)
        active_panels = int(getattr(s1, "active_panels", 0) or 0)

        # Истёкшие панели (по expires_at)
        r2: Result = await db.execute(
            text(
                f"""
                SELECT COUNT(1) AS expired_panels
                FROM {SCHEMA_CORE}.panels
                WHERE expires_at < NOW()
                """
            )
        )
        s2 = r2.fetchone() or {}
        expired_panels = int(getattr(s2, "expired_panels", 0) or 0)

        # Аномалия: last_generated_at > expires_at
        r3: Result = await db.execute(
            text(
                f"""
                SELECT COUNT(1) AS cnt
                FROM {SCHEMA_CORE}.panels
                WHERE last_generated_at IS NOT NULL
                  AND expires_at IS NOT NULL
                  AND last_generated_at > expires_at
                """
            )
        )
        s3 = r3.fetchone() or {}
        anomalous_last_gt_expiry = int(getattr(s3, "cnt", 0) or 0)

        # Аномалия: last_generated_at в будущем
        r4: Result = await db.execute(
            text(
                f"""
                SELECT COUNT(1) AS cnt
                FROM {SCHEMA_CORE}.panels
                WHERE last_generated_at IS NOT NULL
                  AND last_generated_at > NOW()
                """
            )
        )
        s4 = r4.fetchone() or {}
        anomalous_last_in_future = int(getattr(s4, "cnt", 0) or 0)

        # Аномалия: is_active = TRUE, но expires_at < NOW()
        r5: Result = await db.execute(
            text(
                f"""
                SELECT COUNT(1) AS cnt
                FROM {SCHEMA_CORE}.panels
                WHERE is_active = TRUE
                  AND expires_at < NOW()
                """
            )
        )
        s5 = r5.fetchone() or {}
        anomalous_expired_but_active = int(getattr(s5, "cnt", 0) or 0)

        return PanelsHealthSnapshot(
            total_panels=total_panels,
            active_panels=active_panels,
            expired_panels=expired_panels,
            anomalous_last_gt_expiry=anomalous_last_gt_expiry,
            anomalous_last_in_future=anomalous_last_in_future,
            anomalous_expired_but_active=anomalous_expired_but_active,
        )


__all__ = [
    "PanelAdminError",
    "PanelLimitExceeded",
    "PanelNotFound",
    "PanelToggleRequest",
    "PanelBrief",
    "PanelsHealthSnapshot",
    "AdminPanelsService",
]

