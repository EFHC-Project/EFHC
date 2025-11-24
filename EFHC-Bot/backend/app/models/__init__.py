# -*- coding: utf-8 -*-
# backend/app/models/__init__.py
# =============================================================================
# Назначение кода:
# Единая точка входа слоя моделей EFHC Bot. Централизует:
#  • загрузку ORM-базиса (Base, схема БД),
#  • безопасное авто-обнаружение моделей из подмодулей,
#  • реестр MODEL_REGISTRY для удобного доступа к классам моделей,
#  • лёгкую ИИ-диагностику полноты набора таблиц (models_health).
#
# Канон/инварианты (важно):
#  • Модели описывают структуру данных, НЕ содержат бизнес-логики и денег.
#  • Денежные операции выполняются ТОЛЬКО в services/transactions_service.py.
#  • Имена и точности денежных/энергетических полей определяются в самих
#    моделях (Decimal с 8 знаками), а конфигурация — в core/config_core.py.
#
# ИИ-защита/самовосстановление:
#  • Автоимпорт модулей с «мягкими» try/except: отсутствие части файлов
#    не «роняет» процесс — реестр строится из доступных моделей.
#  • models_health() возвращает детальный отчёт: какие ключевые сущности
#    найдены, каких нет, и почему это критично.
#
# Запреты:
#  • Не размещать в __init__ бизнес-операции, миграции, DDL/DML и «create_all()».
#  • Не дублировать конфиг-значения — используем core/config_core.get_settings().
# =============================================================================

from __future__ import annotations

import importlib
import inspect
from typing import Dict, List, Optional, Tuple, Type

from ..core.config_core import get_settings
from ..core.logging_core import get_logger
from ..core.database_core import Base  # единый Declarative Base проекта

logger = get_logger(__name__)
settings = get_settings()
SCHEMA: str = getattr(settings, "DB_SCHEMA_CORE", "efhc_core")

# -----------------------------------------------------------------------------
# Автоподключение модулей с моделями
# -----------------------------------------------------------------------------
# Примечание: в проекте встречались разные наименования файлов (ед/мн. числа).
# Чтобы не «ронять» старт на ранних этапах, пробуем разумные варианты.
_MODEL_MODULE_CANDIDATES: List[str] = [
    # ядро пользователей/транзакций/TON-логов
    "user_models",
    "transactions_models",
    "ton_logs_models",
    "bank_models",
    # панели/архив/рейтинг/достижения/вывод
    "panels_models",
    "rating_models",
    "achievements_models",
    "withdraw_models",
    # рефералы (ед/мн. число — поддерживаем оба файла)
    "referrals_models",
    "referral_models",
    # магазин/заказы/лотереи (ед/мн. число — поддерживаем оба файла)
    "shop_models",
    "order_models",
    "orders_models",
    "lottery_models",
    "lotteries_models",
    # админ-таблицы
    "admin_models",
]

def _safe_import(module_name: str):
    """Пытается импортировать backend.app.models.<module_name>. Возвращает модуль или None."""
    full = f"backend.app.models.{module_name}"
    try:
        mod = importlib.import_module(full)
        logger.debug("models: imported %s", full)
        return mod
    except Exception as e:
        logger.debug("models: skip %s (%s)", full, e)
        return None

def _collect_model_classes(module) -> Dict[str, Type[Base]]:
    """
    Возвращает {ClassName: Class} для всех классов-моделей SQLAlchemy (подклассы Base)
    с объявленным __tablename__.
    """
    registry: Dict[str, Type[Base]] = {}
    if not module:
        return registry
    for name, obj in vars(module).items():
        if inspect.isclass(obj) and issubclass(obj, Base) and hasattr(obj, "__tablename__"):
            registry[name] = obj
    return registry

# -----------------------------------------------------------------------------
# Строим глобальный реестр моделей
# -----------------------------------------------------------------------------
MODEL_REGISTRY: Dict[str, Type[Base]] = {}
MODEL_MODULES_LOADED: List[str] = []

for candidate in _MODEL_MODULE_CANDIDATES:
    mod = _safe_import(candidate)
    if not mod:
        continue
    classes = _collect_model_classes(mod)
    if classes:
        MODEL_REGISTRY.update(classes)
        MODEL_MODULES_LOADED.append(candidate)

__all__ = [
    "Base",
    "SCHEMA",
    "MODEL_REGISTRY",
    "MODEL_MODULES_LOADED",
    "get_model",
    "list_models",
    "models_health",
]

# -----------------------------------------------------------------------------
# Публичные утилиты реестра
# -----------------------------------------------------------------------------
def get_model(name: str) -> Optional[Type[Base]]:
    """
    Возвращает класс модели по имени (как объявлен в Python, а не __tablename__).
    Пример: get_model("User") → <class User> или None.
    """
    return MODEL_REGISTRY.get(name)

def list_models() -> List[Tuple[str, str]]:
    """
    Возвращает список пар (ClassName, __tablename__) всех обнаруженных моделей.
    Удобно для админ-диагностики.
    """
    result: List[Tuple[str, str]] = []
    for cls_name, cls in sorted(MODEL_REGISTRY.items(), key=lambda kv: kv[0].lower()):
        table = getattr(cls, "__tablename__", "?")
        result.append((cls_name, table))
    return result

# -----------------------------------------------------------------------------
# ИИ-диагностика полноты набора таблиц
# -----------------------------------------------------------------------------
def models_health() -> Dict[str, object]:
    """
    Проверяет наличие критически важных сущностей/таблиц.
    Возвращает словарь вида:
      {
        "ok": bool,
        "missing_classes": [ .. ],
        "missing_tables":  [ .. ],
        "loaded_modules":  [ .. ],
        "present": [(ClassName, tablename), ..]
      }

    Политика проверок (минимально достаточный базис):
      • User             — учёт пользователя, балансы (main/bonus), kWh.
      • EFHCTransferLog  — журнал банка EFHC (idempotency_key UNIQUE).
      • TonInboxLog      — журнал входящих TON (tx_hash UNIQUE).
      • Panel / PanelArchive — панели и архив (180 дней).
      • ShopOrder        — магазинные заказы (EFHC пакеты, NFT заявки).
    """
    required_class_names = [
        "User",
        "EFHCTransferLog",
        "TonInboxLog",
        # допускаем разные имена таблиц панелей:
        "Panel",
        "PanelArchive",
        "ShopOrder",
    ]

    present_pairs = list_models()
    present_classes = {cls for cls, _ in present_pairs}
    missing_classes = [rc for rc in required_class_names if rc not in present_classes]

    # Пытаемся обнаружить «типичные» имена таблиц — полезно для ранних ревизий
    present_tables = {tbl for _, tbl in present_pairs}
    expected_tables_hints = {
        "User": ["users"],
        "EFHCTransferLog": ["efhc_transfers_log", "transfers_log"],
        "TonInboxLog": ["ton_inbox_logs", "ton_logs", "ton_inbox"],
        "Panel": ["panels"],
        "PanelArchive": ["panels_archive", "panel_archive"],
        "ShopOrder": ["shop_orders", "orders"],
    }
    missing_tables: List[str] = []
    for cls, hints in expected_tables_hints.items():
        if cls in missing_classes:
            # не проверяем таблицу, если не найден класс
            continue
        if not any(h in present_tables for h in hints):
            missing_tables.extend(hints)

    ok = (len(missing_classes) == 0)

    report = {
        "ok": ok,
        "missing_classes": missing_classes,
        "missing_tables": missing_tables,
        "loaded_modules": list(MODEL_MODULES_LOADED),
        "present": present_pairs,
        "schema": SCHEMA,
    }
    if not ok:
        logger.warning("models_health: missing=%s tables_hint=%s", missing_classes, missing_tables)
    else:
        logger.info("models_health: OK, %d models in %d modules",
                    len(present_pairs), len(MODEL_MODULES_LOADED))
    return report

# =============================================================================
# Пояснения:
# • MODEL_REGISTRY — «единое место правды» для классов моделей. Сервисы/CRUD
#   могут безопасно получать ссылки на классы без жёстких import-цепочек.
# • В проекте сохраняем Alembic-миграции для создания/изменения таблиц. Этот
#   пакет НЕ выполняет DDL сам (никаких create_all), чтобы не ломать миграции.
# • Если переименуете файлы моделей (ед/мн. число), добавьте имя в
#   _MODEL_MODULE_CANDIDATES для мягкой совместимости между ветками.
# =============================================================================
