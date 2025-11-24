# -*- coding: utf-8 -*-
# backend/app/core/__init__.py
# =============================================================================
# Назначение кода:
# Единая точка входа ядра EFHC Bot: загрузка настроек, первичная инициализация
# логирования, запуск проверок «канона» (system_locks) и безопасный экспорт
# ключевых утилит ядра во внешние модули (сервисы, роуты, планировщик).
#
# Канон/инварианты (важно):
# • Источником истины служит config_core.get_settings() — никаких локальных
#   дублей констант здесь не создаём.
# • Проверки «канона» выполняются при старте через system_locks.* (VIP/GEN/sec,
#   запрет P2P/обратной конверсии, требование Idempotency-Key для денежных POST).
# • Денежные операции здесь НЕ выполняются (только конфиг/проверки/экспорты).
#
# ИИ-защита/самовосстановление:
# • boot_core() мягко и устойчиво запускает набор стартовых проверок и всегда
#   возвращает диагностический словарь — без падения всего процесса.
# • _run_system_locks() адаптивно ищет доступную функцию в system_locks
#   (enforce_canon_or_raise / validate_canon_or_raise / startup_check / run).
# • core_health() проверяет «минимально достаточный» набор настроек и
#   возвращает подробный отчёт (ok + список ошибок/варнингов).
#
# Запреты:
# • Не определяем здесь бизнес-логики и не импортируем тяжёлые слои (CRUD/Services).
# • Не дублируем числовые ставки/константы — используем только settings.
# =============================================================================

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .config_core import get_settings  # единый источник настроек (Pydantic)
from .logging_core import get_logger   # унификация логирования по проекту
from . import system_locks              # проверки «канона» и запретов на старте
from . import security_core             # мини-Depends и базовые политики безопасности
from . import utils_core                # общие безопасные утилиты ядра

# Версия ядра (повышать при несовместимых изменениях ядра)
CORE_VERSION = "1.0.0"

logger = get_logger(__name__)

__all__ = [
    "CORE_VERSION",
    "get_settings",
    "logger",
    "boot_core",
    "core_health",
    "security_core",
    "utils_core",
]

# -----------------------------------------------------------------------------
# Внутренние помощники (адаптивные, чтобы не «ронять» процесс при эволюции API)
# -----------------------------------------------------------------------------

def _run_system_locks(settings) -> Dict[str, Any]:
    """
    Запускает стартовые проверки «канона», выбирая доступную функцию
    из модуля system_locks. Возвращает диагностический словарь.
    """
    candidates = [
        "enforce_canon_or_raise",
        "validate_canon_or_raise",
        "startup_check",
        "run",
    ]
    for name in candidates:
        fn = getattr(system_locks, name, None)
        if callable(fn):
            try:
                fn(settings)  # предполагается, что при нарушении бросит исключение
                return {"ok": True, "runner": name, "error": None}
            except Exception as e:
                logger.exception("System locks failed via %s: %s", name, e)
                return {"ok": False, "runner": name, "error": repr(e)}
    # Если ничего не нашли — это не крит, но сообщим (для ранних этапов сборки)
    warn = "No startup system_locks runner found; checks were not executed."
    logger.warning(warn)
    return {"ok": False, "runner": None, "error": warn}


def _safe_num(val: Any, kind: str) -> Optional[float]:
    """Пытается привести значение к float для базовых sanity-checks."""
    try:
        return float(val)
    except Exception:
        logger.warning("core_health: %s is not numeric (%r)", kind, val)
        return None


# -----------------------------------------------------------------------------
# Публичные функции инициализации/диагностики
# -----------------------------------------------------------------------------

def boot_core() -> Dict[str, Any]:
    """
    Назначение кода:
        Безопасная инициализация ядра EFHC Bot. Загружает настройки, запускает
        проверки «канона», пишет диагностический лог и возвращает сводку.

    Возвращает:
        dict c ключами:
          • timestamp_utc — ISO-время старта,
          • core_version  — версия ядра,
          • health        — результат core_health(),
          • locks         — результат _run_system_locks().

    Побочные эффекты:
        • Логи старта/варнингов.
        • Никаких операций с БД/деньгами — только конфиг и проверки.
    """
    ts = datetime.now(timezone.utc).isoformat()
    settings = get_settings()
    logger.info("EFHC Core boot: version=%s env=%s schema=%s",
                CORE_VERSION, getattr(settings, "ENVIRONMENT", "unknown"),
                getattr(settings, "DB_SCHEMA_CORE", "efhc_core"))

    health = core_health()
    locks = _run_system_locks(settings)

    if not health.get("ok"):
        logger.warning("Core health warnings: %s", health.get("errors"))

    if not locks.get("ok"):
        # Не «роняем» процесс — отдаём сводку. Админка увидит ошибку.
        logger.error("System locks did not pass: %s", locks.get("error"))

    return {
        "timestamp_utc": ts,
        "core_version": CORE_VERSION,
        "health": health,
        "locks": locks,
    }


def core_health() -> Dict[str, Any]:
    """
    Назначение кода:
        Выполняет быстрые sanity-checks по ключевым настройкам. Никаких падений —
        только отчёт для логов/админки.

    Проверяем:
        • Наличие GEN_PER_SEC_BASE_KWH и GEN_PER_SEC_VIP_KWH (pos-сек ставки).
        • ADMIN_BANK_TELEGRAM_ID — задан (банк обязателен).
        • DB_SCHEMA_CORE — задана схема.
        • SCHEDULER_TICK_SECONDS — положительное число.

    Возвращает:
        dict: { ok: bool, errors: List[str], snapshot: Dict[str, Any] }
    """
    settings = get_settings()
    errors: List[str] = []

    base = getattr(settings, "GEN_PER_SEC_BASE_KWH", None)
    vip = getattr(settings, "GEN_PER_SEC_VIP_KWH", None)
    admin_bank = getattr(settings, "ADMIN_BANK_TELEGRAM_ID", None)
    schema = getattr(settings, "DB_SCHEMA_CORE", None)
    tick = getattr(settings, "SCHEDULER_TICK_SECONDS", None)

    base_num = _safe_num(base, "GEN_PER_SEC_BASE_KWH")
    vip_num = _safe_num(vip, "GEN_PER_SEC_VIP_KWH")
    tick_num = _safe_num(tick, "SCHEDULER_TICK_SECONDS")

    if base_num is None or base_num <= 0:
        errors.append("GEN_PER_SEC_BASE_KWH must be positive numeric.")
    if vip_num is None or vip_num <= 0:
        errors.append("GEN_PER_SEC_VIP_KWH must be positive numeric.")
    if admin_bank in (None, "", 0):
        errors.append("ADMIN_BANK_TELEGRAM_ID must be set (bank identity required).")
    if not schema:
        errors.append("DB_SCHEMA_CORE must be set.")
    if tick_num is None or tick_num <= 0:
        errors.append("SCHEDULER_TICK_SECONDS must be positive numeric.")

    ok = len(errors) == 0

    snapshot = {
        # ВНИМАНИЕ: не выводим чувствительные данные (ключи/пароли/URL целиком)
        "PROJECT_NAME": getattr(settings, "PROJECT_NAME", None),
        "ENVIRONMENT": getattr(settings, "ENVIRONMENT", None),
        "DB_SCHEMA_CORE": schema,
        "GEN_PER_SEC_BASE_KWH": base,
        "GEN_PER_SEC_VIP_KWH": vip,
        "SCHEDULER_TICK_SECONDS": tick,
        "STRICT_IDEMPOTENCY": getattr(settings, "STRICT_IDEMPOTENCY", None),
        "FORBID_NEGATIVE_BANK_BALANCE": getattr(settings, "FORBID_NEGATIVE_BANK_BALANCE", None),
    }

    return {"ok": ok, "errors": errors, "snapshot": snapshot}


# =============================================================================
# Пояснения:
# • Этот __init__ не дублирует конфиг — только экспортирует get_settings и
#   предоставляет безопасные boot_core()/core_health() для старта и диагностики.
# • boot_core() следует вызывать в main.py при инициализации приложения и/или
#   в entry-point планировщика, чтобы оперативно увидеть проблемы конфигурации.
# • Если в будущем изменится API system_locks, адаптер _run_system_locks()
#   позволит ядру стартовать без «жёстких падений», а админка увидит отчёт.
# =============================================================================
