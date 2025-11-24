# -*- coding: utf-8 -*-
# backend/app/routes/__init__.py
# =============================================================================
# Назначение кода:
#   Единая точка подключения всех HTTP-роутов EFHC Bot. Этот модуль агрегирует
#   подмодули роутов и предоставляет:
#     • общий APIRouter (api_router), в который «вмонтированы» все найденные роуты;
#     • функцию register(app, prefix="") для удобного подключения в FastAPI;
#     • диагностику подключённых/пропущенных модулей для наблюдаемости.
#
# Канон/инварианты:
#   • Этот модуль НЕ выполняет бизнес-логику и НЕ трогает деньги — только проводка
#     маршрутов. Любые денежные правила (Idempotency-Key, банк и пр.) реализуются
#     в самих роут-модулях и сервисах.
#   • Поддерживается «мягкая деградация»: отсутствие части модулей роутов не должно
#     «ронять» процесс (полезно при поэтапном деплое/миграциях).
#   • Никаких суточных расчётов/констант здесь нет и быть не должно.
#
# ИИ-защита/самовосстановление:
#   • Мягкие импорты: каждый модуль подключается через try/except; при неудаче
#     пишем предупреждение в лог и двигаемся дальше.
#   • Диагностика: храним список успешно присоединённых/пропущенных роутов,
#     доступны функции list_registered_routes() и list_missing_routes().
#   • Избегаем побочных эффектов при импорте: только сборка маршрутов.
#
# Запреты:
#   • Нет прямых SQL, нет вызовов сервисов — только import и include_router.
#   • Нет динамического изменения префиксов дочерних роутеров — каждый модуль
#     сам содержит свой prefix (например, "/user", "/panels", "/api/admin"...).
# =============================================================================

from __future__ import annotations

from importlib import import_module
from typing import List, Tuple

from fastapi import APIRouter, FastAPI

from backend.app.core.logging_core import get_logger

logger = get_logger(__name__)

# -----------------------------------------------------------------------------
# Перечень ожидаемых модулей роутов (по канонической схеме)
#   Порядок важен для читабельности / предсказуемости логов.
#   Каждый модуль должен экспортировать переменную `router: APIRouter`.
# -----------------------------------------------------------------------------
ROUTERS_EXPECTED: Tuple[str, ...] = (
    # user и витрины
    "user_routes",
    "panels_routes",
    "exchange_routes",
    "rating_routes",
    "referrals_routes",
    "shop_routes",
    "lotteries_routes",
    "withdraw_routes",
    "tasks_routes",
    # админские разделы
    "admin_routes",
    "admin_referral_routes",
    "admin_tasks_routes",
)

# -----------------------------------------------------------------------------
# Глобальный агрегатор маршрутов. В main.py обычно делают:
#     from backend.app.routes import register
#     app = FastAPI(...)
#     register(app, prefix="")  # либо prefix="/api"
# -----------------------------------------------------------------------------
api_router = APIRouter()

# Внутренняя телеметрия: что подключили/что нет.
_ATTACHED: List[str] = []
_MISSING: List[str] = []


def _try_include(module_basename: str) -> None:
    """
    Пытаемся импортировать модуль роутов и смонтировать его `router` в api_router.
    Ошибка импорта не фатальна — логируем и продолжаем.
    """
    fqmn = f"backend.app.routes.{module_basename}"
    try:
        mod = import_module(fqmn)
    except Exception as e:
        logger.warning("routes: пропущен модуль %s (импорт не удался): %s", fqmn, e)
        _MISSING.append(module_basename)
        return

    router = getattr(mod, "router", None)
    if not isinstance(router, APIRouter):
        logger.warning("routes: пропущен модуль %s (нет router: APIRouter)", fqmn)
        _MISSING.append(module_basename)
        return

    try:
        api_router.include_router(router)
        _ATTACHED.append(module_basename)
        logger.info("routes: подключён модуль %s", fqmn)
    except Exception as e:
        logger.error("routes: ошибка include_router для %s: %s", fqmn, e)
        _MISSING.append(module_basename)


# При импорте модуля собираем доступные роуты (мягко).
for _name in ROUTERS_EXPECTED:
    _try_include(_name)


# -----------------------------------------------------------------------------
# Публичные утилиты
# -----------------------------------------------------------------------------
def register(app: FastAPI, prefix: str = "") -> None:
    """
    Регистрирует агрегированный роутер в приложении FastAPI.

    Args:
        app:   экземпляр FastAPI.
        prefix: базовый префикс для всех маршрутов (обычно "" или "/api").
    """
    app.include_router(api_router, prefix=prefix)
    logger.info(
        "routes: зарегистрирован агрегатор (prefix=%r). Подключено: %s. Пропущено: %s.",
        prefix,
        ",".join(_ATTACHED) if _ATTACHED else "-",
        ",".join(_MISSING) if _MISSING else "-",
    )


def list_registered_routes() -> List[str]:
    """
    Возвращает список коротких имён модулей роутов, которые были успешно подключены.
    Полезно для health-диагностики админки.
    """
    return list(_ATTACHED)


def list_missing_routes() -> List[str]:
    """
    Возвращает список коротких имён модулей роутов, которые не удалось подключить.
    Основания: ошибка импорта модуля или отсутствие `router: APIRouter`.
    """
    return list(_MISSING)


__all__ = [
    "api_router",
    "register",
    "list_registered_routes",
    "list_missing_routes",
    "ROUTERS_EXPECTED",
]
