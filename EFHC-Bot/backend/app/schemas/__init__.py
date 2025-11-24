# -*- coding: utf-8 -*-
# backend/app/schemas/__init__.py
# =============================================================================
# Назначение кода:
# Централизованный «фасад» для Pydantic-схем EFHC Bot. Даёт единый импорт:
#     from backend.app.schemas import ShopCatalogOut, TaskOut, ...
# Подхватывает публичные символы из модулей-схем и экспортирует их наружу.
#
# Канон / инварианты:
# • Здесь НЕТ бизнес-логики и вычислений балансов/денег — только агрегация схем.
# • Имена экспортируются из подмодулей строго по их __all__ (если задан) или
#   по правилу: «не начинать с '_'».
# • При конфликте имён между модулями — логируем предупреждение и НЕ перезаписываем
#   уже экспортированное имя (ИИ-защита от «тихого» переопределения типов).
#
# ИИ-защиты / самовосстановление:
# • Динамический сбор экспортов: если какой-то модуль временно отсутствует,
#   импорт не «роняет» всё пространство — пропускаем с мягким предупреждением.
# • Предоставляем утилиту get_public_exports() для быстрой диагностики: что именно
#   экспортируется наружу, из какого модуля и сколько символов.
#
# Запреты:
# • Никаких сторонних зависимостей, сетевых вызовов и доступа к БД.
# • Никаких «временных фиксов»/TODO — только чистая агрегация.
# =============================================================================

from __future__ import annotations

from importlib import import_module
from typing import Dict, List, Tuple

try:
    # Логгер проекта (не обязателен, но полезен для предупреждений о дубликатах)
    from backend.app.core.logging_core import get_logger
    _logger = get_logger(__name__)
except Exception:  # pragma: no cover
    # Фолбэк: если логгер ещё не готов — используем заглушку print
    class _StubLogger:
        def warning(self, *args, **kwargs):  # noqa: D401
            """print-fallback for warnings"""
            try:
                print("[schemas:init][warn]", *args)
            except Exception:
                pass

        def info(self, *args, **kwargs):
            try:
                print("[schemas:init][info]", *args)
            except Exception:
                pass

    _logger = _StubLogger()  # type: ignore


SCHEMAS_VERSION: str = "v1.0"  # версионирование фасада схем

# Порядок важен: чем раньше модуль — тем выше приоритет его имён при конфликте.
# Добавляйте новые модули сюда, чтобы они подключались автоматически.
_SCHEMA_MODULES_ORDERED: List[str] = [
    "backend.app.schemas.common_schemas",
    "backend.app.schemas.user_schemas",
    "backend.app.schemas.panels_schemas",
    "backend.app.schemas.exchange_schemas",
    "backend.app.schemas.shop_schemas",
    "backend.app.schemas.tasks_schemas",
    "backend.app.schemas.referrals_schemas",
    "backend.app.schemas.rating_schemas",
    "backend.app.schemas.orders_schemas",
    "backend.app.schemas.lotteries_schemas",
    "backend.app.schemas.transactions_schemas",
    "backend.app.schemas.ads_schemas",
]

# Хранилище служебной информации об экспортированных именах:
#   name -> (module_name, object_ref)
_export_registry: Dict[str, Tuple[str, object]] = {}

# Итоговый публичный список для `from backend.app.schemas import *`
__all__: List[str] = []


def _collect_public_names(module) -> List[str]:
    """
    Собирает публичные имена из модуля:
      • если определён __all__ — берём только его;
      • иначе — все атрибуты, не начинающиеся с '_'.
    """
    names: List[str]
    if hasattr(module, "__all__"):
        try:
            names = [n for n in list(module.__all__) if isinstance(n, str)]
        except Exception:
            names = [n for n in dir(module) if not n.startswith("_")]
    else:
        names = [n for n in dir(module) if not n.startswith("_")]
    return names


def _safe_register(name: str, module_name: str, value: object) -> None:
    """
    Регистрирует публичное имя в глобальном пространстве __init__.py.
    Если имя уже занято (конфликт), НЕ перезаписывает — логирует предупреждение.
    """
    if name in _export_registry:
        prev_module, _ = _export_registry[name]
        _logger.warning(
            "Конфликт имён схем: %s (из %s) уже экспортирован; пропущено дублирующее из %s",
            name, prev_module, module_name
        )
        return
    globals()[name] = value
    _export_registry[name] = (module_name, value)
    __all__.append(name)


# ----- Динамический сбор экспортов из модулей схем ---------------------------
for mod_path in _SCHEMA_MODULES_ORDERED:
    try:
        mod = import_module(mod_path)
    except Exception as e:  # pragma: no cover
        _logger.warning("Модуль схем не загружен: %s (%s)", mod_path, e)
        continue

    for public_name in _collect_public_names(mod):
        try:
            _safe_register(public_name, mod_path, getattr(mod, public_name))
        except Exception as e:  # pragma: no cover
            _logger.warning(
                "Не удалось экспортировать символ %s из %s: %s",
                public_name, mod_path, e
            )

# Итоговый __all__ приводим к детерминированному порядку
__all__ = sorted(set(__all__))


# ------------------------- Диагностические утилиты ---------------------------
def get_public_exports() -> Dict[str, Dict[str, int | List[str]]]:
    """
    Возвращает диагностическую сводку по экспортам фасада:
      {
        "version": "v1.0",
        "modules": N,
        "symbols": M,
        "by_module": {
           "backend.app.schemas.shop_schemas": ["ShopCatalogOut", ...],
           ...
        }
      }
    Удобно для healthcheck админки и unit-тестов.
    """
    by_module: Dict[str, List[str]] = {}
    for name, (mod, _obj) in _export_registry.items():
        by_module.setdefault(mod, []).append(name)

    # Сортируем для стабильности вывода
    for names in by_module.values():
        names.sort()

    return {
        "version": SCHEMAS_VERSION,
        "modules": len(_SCHEMA_MODULES_ORDERED),
        "symbols": len(__all__),
        "by_module": {k: v for k, v in sorted(by_module.items())},
    }


# =============================================================================
# Пояснения «для чайника»:
# • Зачем этот файл?
#   Чтобы фронту/роутам не импортировать десятки модулей, а получать схемы из
#   одного места: `from backend.app.schemas import <SchemaName>`.
#
# • Что будет при одинаковых именах в разных модулях?
#   Первым делом экспортируется модуль, стоящий выше в _SCHEMA_MODULES_ORDERED.
#   Если позже встретится имя-конфликт — мы логируем предупреждение и пропускаем
#   повтор, НЕ перезаписывая уже доступный символ.
#
# • А если модуль временно отсутствует?
#   Мы просто пропускаем его (мягкая деградация), остальная часть схем
#   останется доступной. Это помогает переживать неполные сборки.
#
# • Как посмотреть, что экспортировано?
#   Вызовите schemas.get_public_exports() и посмотрите сводку по модулям/именам.
# =============================================================================
