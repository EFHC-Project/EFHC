# -*- coding: utf-8 -*-
# backend/app/services/__init__.py
# =============================================================================
# EFHC Bot — сервисный слой (единая точка входа)
# -----------------------------------------------------------------------------
# Назначение файла:
#   • Дать единый, стабильный вход для всех основных доменных сервисов EFHC Bot.
#   • Явно зафиксировать «канонический» набор сервисов, чтобы роуты/воркеры
#     не бегали по отдельным файлам.
#   • Предоставить лёгкие ИИ-обёртки:
#       - агрегированный health-snapshot по ключевым сервисам,
#       - глобальный ensure_consistency() для фоновых задач (банк/обмен/вывод).
#
# Важные принципы:
#   • Никакой бизнес-логики здесь нет — только импорты и тонкие прокси.
#   • Никаких сторонних HTTP-запросов/блокирующих операций на уровне импорта.
#   • Все тяжёлые операции (генерация, обмен, вывод, банк) реализованы
#     в отдельных модулях и сюда только импортируются.
# =============================================================================

from __future__ import annotations

from typing import Any, Dict, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.logging_core import get_logger

# Базовые сервисы домена
from .energy_service import (  # noqa: F401
    generate_energy_tick,
    generate_energy_for_user,
    backfill_all as energy_backfill_all,
)
from .exchange_service import (  # noqa: F401
    preview_exchange,
    request_exchange,
    max_exchangeable_kwh,
    user_balances_brief,
    health_snapshot as exchange_health_snapshot,
    ExchangeError,
    ExchangeValidationError,
    ExchangeDirectionForbidden,
    ExchangeInsufficientEnergy,
    ExchangeIdempotencyRequired,
)
from .withdraw_service import (  # noqa: F401
    request_withdraw,
    cancel_withdraw,
    admin_approve_withdraw,
    admin_reject_withdraw,
    admin_mark_paid,
    list_user_withdraws,
    ensure_consistency as withdraw_ensure_consistency,
)
from .transactions_service import (  # noqa: F401
    credit_user_from_bank,
    credit_user_bonus_from_bank,
    debit_user_to_bank,
    debit_user_bonus_to_bank,
    exchange_kwh_to_efhc,
)

# Админ-подсистема (фасад и подмодули)
from .admin_service import AdminService  # noqa: F401

from .admin.admin_rbac import (  # noqa: F401
    AdminRole,
    AdminUser,
    AdminAuthError,
)
from .admin.admin_logging import (  # noqa: F401
    AdminLog,
    AdminLogger,
)
from .admin.admin_settings import (  # noqa: F401
    SettingsService,
    SystemSetting,
)
from .admin.admin_notifications import (  # noqa: F401
    AdminNotification,
    AdminNotifier,
)
from .admin.admin_bank_service import (  # noqa: F401
    AdminBankService,
)
from .admin.admin_lotteries_service import (  # noqa: F401
    AdminLotteriesService,
)
from .admin.admin_users_service import (  # noqa: F401
    AdminUsersService,
)
from .admin.admin_panels_service import (  # noqa: F401
    AdminPanelsService,
)
from .admin.admin_referral_service import (  # noqa: F401
    AdminReferralService,
)
from .admin.admin_wallets_service import (  # noqa: F401
    AdminWalletsService,
)
from .admin.admin_stats_service import (  # noqa: F401
    AdminStatsService,
)

logger = get_logger(__name__)

# =============================================================================
# Глобальные ИИ-утилиты: health-сводка и ensure_consistency для фоновых задач
# =============================================================================


async def services_health_snapshot(db: AsyncSession) -> Dict[str, Any]:
    """
    Лёгкий агрегированный health-snapshot по основным сервисам.

    Задача:
      • не падать из-за ошибок одного сервиса;
      • дать админ-панели/мониторингу единый JSON со статусом ядра.

    Сейчас включает:
      • состояние обменника (kWh→EFHC);
      • суммарную доступную энергию в системе.

    При необходимости можно расширять (энергия, банк, выводы и т.д.),
    но только через лёгкие read-only запросы.
    """
    out: Dict[str, Any] = {
        "exchange": None,
        "errors": [],
    }

    # Обменник (kWh→EFHC)
    try:
        out["exchange"] = await exchange_health_snapshot(db)
    except Exception as e:  # noqa: BLE001
        logger.warning("services_health_snapshot: exchange_health failed: %s", e)
        out["errors"].append({"component": "exchange", "error": str(e)})

    return out


async def ensure_global_consistency(
    db: AsyncSession,
    *,
    scan_minutes: int = 240,
    batch_limit: int = 200,
) -> Dict[str, Any]:
    """
    Глобальная «ИИ-самовосстановление» по сервисам:

      • withdraw_ensure_consistency:
          - подтягивает висящие холды/рефанды по заявкам на вывод EFHC;

    Принципы:
      • Любая ошибка локализуется в своём блоке и только логируется.
      • Возвращаем подробный отчёт по каждому под-сервису, чтобы админ-панель
        могла подсветить проблемные подсистемы.
    """
    result: Dict[str, Any] = {
        "withdraw": None,
        "errors": [],
    }

    # 1) Консистентность заявок на вывод EFHC
    try:
        result["withdraw"] = await withdraw_ensure_consistency(
            db,
            scan_minutes=scan_minutes,
            batch_limit=batch_limit,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("ensure_global_consistency: withdraw.ensure_consistency failed: %s", e)
        result["errors"].append({"component": "withdraw", "error": str(e)})

    return result


# =============================================================================
# Экспортируемый интерфейс пакета backend.app.services
# =============================================================================

__all__ = [
    # --- energy_service ---
    "generate_energy_tick",
    "generate_energy_for_user",
    "energy_backfill_all",
    # --- exchange_service ---
    "preview_exchange",
    "request_exchange",
    "max_exchangeable_kwh",
    "user_balances_brief",
    "ExchangeError",
    "ExchangeValidationError",
    "ExchangeDirectionForbidden",
    "ExchangeInsufficientEnergy",
    "ExchangeIdempotencyRequired",
    # --- withdraw_service ---
    "request_withdraw",
    "cancel_withdraw",
    "admin_approve_withdraw",
    "admin_reject_withdraw",
    "admin_mark_paid",
    "list_user_withdraws",
    "withdraw_ensure_consistency",
    # --- transactions_service ---
    "credit_user_from_bank",
    "credit_user_bonus_from_bank",
    "debit_user_to_bank",
    "debit_user_bonus_to_bank",
    "exchange_kwh_to_efhc",
    # --- admin facade ---
    "AdminService",
    # --- admin core modules ---
    "AdminRole",
    "AdminUser",
    "AdminAuthError",
    "AdminLog",
    "AdminLogger",
    "SettingsService",
    "SystemSetting",
    "AdminNotification",
    "AdminNotifier",
    "AdminBankService",
    "AdminLotteriesService",
    "AdminUsersService",
    "AdminPanelsService",
    "AdminReferralService",
    "AdminWalletsService",
    "AdminStatsService",
    # --- global helpers ---
    "services_health_snapshot",
    "ensure_global_consistency",
]
