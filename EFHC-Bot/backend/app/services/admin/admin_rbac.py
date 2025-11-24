# -*- coding: utf-8 -*-
# backend/app/services/admin/admin_rbac.py
# =============================================================================
# EFHC Bot — RBAC для админ-панели
# -----------------------------------------------------------------------------
# Назначение:
#   • Централизованная проверка прав администраторов.
#   • Безопасное разрешение admin-пользователя по Telegram ID.
#   • Единый «словарь ролей» для всей админ-подсистемы.
#
# ИИ-защита / инварианты:
#   • Любой доступ в админ-панель возможен только при наличии:
#       - валидного admin-пользователя в БД,
#       - активного статуса (is_active = TRUE),
#       - корректной роли из ограниченного списка.
#   • Любые «нестандартные» или повреждённые данные (неизвестная роль,
#     отключенный админ, дубликаты) приводят к отказу в доступе с
#     понятной ошибкой уровня сервиса (AdminAuthError).
#   • Дополнительная защита: при наличии ROOT_ADMIN_TELEGRAM_ID из конфига
#     этот ID всегда трактуется как SuperAdmin (фолбэк при авариях с БД).
#
# Как использовать:
#   • В роутерах:
#       admin = await RBAC.resolve_admin(db, telegram_id)
#       RBAC.require_role(admin, AdminRole.MODERATOR)
#   • Фасад admin_facade.py использует эти функции для всех административных действий.
# =============================================================================

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Literal

from pydantic import BaseModel, Field

from sqlalchemy import text
from sqlalchemy.engine import Result
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config_core import get_settings
from backend.app.core.logging_core import get_logger

logger = get_logger(__name__)
S = get_settings()
SCHEMA_ADMIN: str = getattr(S, "DB_SCHEMA_ADMIN", "efhc_admin")

# Дополнительный «корневой» админ (опционально, может быть 0/None)
ROOT_ADMIN_TELEGRAM_ID: int = int(getattr(S, "ROOT_ADMIN_TELEGRAM_ID", 0) or 0)


# =============================================================================
# Роли и модели
# =============================================================================

class AdminRole(str):
    """
    Роли администраторов в системе.
    ВАЖНО: любые новые роли обязательно должны быть добавлены сюда и
    в _ROLE_LEVEL, иначе будут отклонены ИИ-защитой.
    """

    SUPERADMIN = "SuperAdmin"   # полный доступ
    MODERATOR = "Moderator"     # пользовательские операции/лотереи/заявки, без минтинга/сжигания
    ANALYST = "Analyst"         # только чтение статистики/логов


# Внутренние «уровни» прав (чем больше — тем выше права)
_ROLE_LEVEL: Dict[str, int] = {
    AdminRole.ANALYST: 1,
    AdminRole.MODERATOR: 2,
    AdminRole.SUPERADMIN: 3,
}


class AdminAuthError(PermissionError):
    """
    Ошибка авторизации администратора.
    Используется вместо «сырых» исключений БД, чтобы роуты могли
    выдавать корректные HTTP-ответы (403/401).
    """


class AdminUser(BaseModel):
    """
    Представление администратора для бизнес-логики:
      • id          — внутренний ID в таблице admin_users;
      • telegram_id — Telegram-ID (primary ключ для поиска);
      • role        — роль (SuperAdmin / Moderator / Analyst);
      • is_active   — активен ли админ (по умолчанию True, если колонка отсутствует).
    """
    id: int = Field(...)
    telegram_id: int = Field(...)
    role: Literal["SuperAdmin", "Moderator", "Analyst"]
    is_active: bool = Field(default=True)


# =============================================================================
# Класс RBAC
# =============================================================================

class RBAC:
    """
    Простая, но строгая RBAC-обёртка над таблицей admin_users:

      ТАБЛИЦА (ожидаемая минимальная структура)
        {SCHEMA_ADMIN}.admin_users (
            id           BIGSERIAL PK,
            telegram_id  BIGINT UNIQUE NOT NULL,
            role         TEXT NOT NULL,               -- 'SuperAdmin' | 'Moderator' | 'Analyst'
            is_active    BOOLEAN NOT NULL DEFAULT TRUE,
            created_at   TIMESTAMPTZ,
            updated_at   TIMESTAMPTZ
        )

    Методы:
      • resolve_admin(db, telegram_id) -> AdminUser
            — найти администратора или выбросить AdminAuthError.
      • try_resolve_admin(db, telegram_id) -> Optional[AdminUser]
            — мягкий вариант, возвращает None, если админ не найден/неактивен.
      • require_role(admin, minimal)
            — убедиться, что роль admin «не ниже» требуемой.
    """

    # -------------------------------------------------------------------------
    # Внутренние хелперы
    # -------------------------------------------------------------------------

    @staticmethod
    def _normalize_role(role_raw: str) -> str:
        """
        Нормализует строку роли:
          • обрезает пробелы,
          • проверяет против белого списка.
        Неизвестные роли → AdminAuthError (ИИ-защита от повреждённых данных).
        """
        role = (role_raw or "").strip()
        if role not in _ROLE_LEVEL:
            raise AdminAuthError(f"Неизвестная роль администратора: {role!r}")
        return role

    @staticmethod
    def _level(role: str) -> int:
        return _ROLE_LEVEL.get(role, 0)

    # -------------------------------------------------------------------------
    # Публичные методы
    # -------------------------------------------------------------------------

    @staticmethod
    async def resolve_admin(db: AsyncSession, telegram_id: int) -> AdminUser:
        """
        Возвращает AdminUser по Telegram-ID или выбрасывает AdminAuthError.

        ИИ-защита:
          • Telegram-ID должен быть положительным.
          • Если включён ROOT_ADMIN_TELEGRAM_ID и он совпадает с telegram_id,
            при любой ошибке чтения БД возвращается виртуальный SuperAdmin
            (для аварийного доступа разработчика/владельца).
        """
        if telegram_id <= 0:
            raise AdminAuthError("Некорректный Telegram ID администратора")

        # Особый случай: «корневой» админ
        if ROOT_ADMIN_TELEGRAM_ID and telegram_id == ROOT_ADMIN_TELEGRAM_ID:
            # Попытка прочитать из БД — если не получилось, создаём виртуального
            try:
                admin = await RBAC._fetch_admin_from_db(db, telegram_id)
                if admin is not None:
                    # Если в БД роль ниже SuperAdmin — повышаем до SuperAdmin «на лету»
                    if admin.role != AdminRole.SUPERADMIN:
                        logger.warning(
                            "ROOT_ADMIN_TELEGRAM_ID=%s имеет роль %s в БД, "
                            "повышаем до SuperAdmin на уровне RBAC",
                            telegram_id,
                            admin.role,
                        )
                        admin.role = AdminRole.SUPERADMIN  # type: ignore[assignment]
                    return admin
            except Exception as e:  # noqa: BLE001
                logger.error("RBAC: ошибка чтения ROOT_ADMIN из БД: %s", e)

            # Виртуальный SuperAdmin (fallback, без записи в БД)
            logger.warning(
                "RBAC: используем виртуального ROOT SuperAdmin для telegram_id=%s",
                telegram_id,
            )
            return AdminUser(id=-1, telegram_id=telegram_id, role=AdminRole.SUPERADMIN, is_active=True)

        # Обычный путь
        admin = await RBAC._fetch_admin_from_db(db, telegram_id)
        if admin is None:
            raise AdminAuthError("Администратор не найден или отключён")
        return admin

    @staticmethod
    async def _fetch_admin_from_db(db: AsyncSession, telegram_id: int) -> Optional[AdminUser]:
        """
        Низкоуровневая выборка админа из БД.
        Возвращает AdminUser или None, если админ не найден/неактивен.
        Любые неожиданные ошибки логируются и пробрасываются наверх.
        """
        sql = text(
            f"""
            SELECT id, telegram_id, role,
                   COALESCE(is_active, TRUE) AS is_active
            FROM {SCHEMA_ADMIN}.admin_users
            WHERE telegram_id = :tid
            LIMIT 1
            """
        )
        try:
            r: Result = await db.execute(sql, {"tid": telegram_id})
            row = r.fetchone()
        except Exception as e:  # noqa: BLE001
            logger.error("RBAC: DB error while resolving admin %s: %s", telegram_id, e)
            raise

        if not row:
            return None

        try:
            role = RBAC._normalize_role(str(row.role))
        except AdminAuthError as e:
            # Логируем подозрительные данные и отказываем в доступе
            logger.error(
                "RBAC: админ с telegram_id=%s имеет некорректную роль '%s' в БД",
                telegram_id,
                getattr(row, "role", None),
            )
            raise e

        is_active = bool(getattr(row, "is_active", True))
        if not is_active:
            logger.info("RBAC: админ telegram_id=%s отключён (is_active=FALSE)", telegram_id)
            return None

        return AdminUser(
            id=int(row.id),
            telegram_id=int(row.telegram_id),
            role=role,  # type: ignore[arg-type]
            is_active=is_active,
        )

    @staticmethod
    async def try_resolve_admin(db: AsyncSession, telegram_id: int) -> Optional[AdminUser]:
        """
        Мягкое разрешение админа:
          • возвращает AdminUser или None;
          • НИКОГДА не выбрасывает AdminAuthError — только логирует.
        Подходит для вспомогательных сценариев, когда отсутствие админа не
        является «фатальной» ошибкой (например, пробный доступ).
        """
        try:
            return await RBAC.resolve_admin(db, telegram_id)
        except AdminAuthError as e:
            logger.info("RBAC.try_resolve_admin: отказ в доступе: %s", e)
            return None

    @staticmethod
    def require_role(admin: AdminUser, minimal: str) -> None:
        """
        Проверяет, что роль admin «не ниже» требуемой minimal.

        Примеры:
          RBAC.require_role(admin, AdminRole.ANALYST)
          RBAC.require_role(admin, AdminRole.MODERATOR)
          RBAC.require_role(admin, AdminRole.SUPERADMIN)

        Если прав недостаточно — выбрасывается AdminAuthError.
        """
        # Нормализуем требуемую роль через _normalize_role, чтобы отловить опечатки в коде
        minimal_norm = RBAC._normalize_role(minimal)
        admin_level = RBAC._level(admin.role)
        need_level = RBAC._level(minimal_norm)

        if admin_level < need_level:
            raise AdminAuthError(
                f"Недостаточно прав: требуется роль не ниже {minimal_norm}, у администратора {admin.role}"
            )

    @staticmethod
    def is_superadmin(admin: AdminUser) -> bool:
        """Удобный хелпер для проверок в коде."""
        return admin.role == AdminRole.SUPERADMIN


__all__ = [
    "AdminRole",
    "AdminUser",
    "AdminAuthError",
    "RBAC",
]

