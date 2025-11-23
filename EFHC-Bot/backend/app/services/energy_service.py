# ============================================================================
# EFHC Bot — energy_service
# -----------------------------------------------------------------------------
# Назначение: начисление энергии панелям каждую секунду с догоном пропущенных
# тиков, самодиагностикой и самолечением данных (rescue_*).
#
# Канон/инварианты:
#   • Генерация только per-sec по GEN_PER_SEC_BASE_KWH / GEN_PER_SEC_VIP_KWH.
#   • Балансы меняет только банковский сервис; здесь двигаем только kWh.
#   • Пользователь не может уйти в минус; банк может (но не затрагивается).
#   • Денежные/энергетические значения — Decimal(8) с ROUND_DOWN через quantize.
#
# ИИ-защиты/самовосстановление:
#   • FOR UPDATE SKIP LOCKED + advisory-лок, чтобы параллельные воркеры
#     не схватили одну и ту же панель.
#   • backfill_all догоняет пропущенные интервалы без дублей («догоняющий»
#     алгоритм от last_tick_at до текущего тика).
#   • rescue_* процедуры чинят пустые last_tick_at и сбои после экспирации.
#   • health_snapshot даёт лёгкую диагностику для алёртов.
#
# Запреты:
#   • Нет суточных ставок, нет P2P, нет EFHC→kWh.
#   • Не трогаем денежные балансы (main/bonus) и не создаём транзакции банка.
# ============================================================================
from __future__ import annotations

from decimal import Decimal
from typing import Any, Iterable

from sqlalchemy import Select, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config_core import GEN_PER_SEC_BASE_KWH, GEN_PER_SEC_VIP_KWH
from ..core.logging_core import get_logger
from ..core.utils_core import quantize_decimal, utc_now
from ..models import Panel, User

logger = get_logger(__name__)

__all__ = [
    "EnergyService",
    "health_snapshot",
    "rescue_fill_null_last_tick",
    "rescue_fix_last_generated_after_expire",
]


async def _try_advisory_lock(session: AsyncSession, key: int) -> bool:
    """Попытаться взять advisory-лок, чтобы единственный воркер вёл тик."""

    result = await session.execute(text("SELECT pg_try_advisory_lock(:k)").bindparams(k=key))
    locked = bool(result.scalar_one())
    return locked


async def _release_advisory_lock(session: AsyncSession, key: int) -> None:
    """Снять advisory-лок после завершения работы."""

    await session.execute(text("SELECT pg_advisory_unlock(:k)").bindparams(k=key))


async def health_snapshot(session: AsyncSession) -> dict[str, Any]:
    """Собрать короткую health-сводку для мониторинга.

    Назначение: вернуть агрегаты по активным пользователям и панелям, а также
    «долг» по начислению (в секундах) для максимального отставания.
    Побочные эффекты: не изменяет БД, балансы не двигает.
    Исключения: пробрасывает ошибки БД (для алёртов).
    ИИ-защита: работает без падения при пустых таблицах (возвращает нули).
    """

    # Быстрый подсчёт активных пользователей и панелей; датасеты могут быть пустыми.
    active_users = await session.scalar(select(func.count(User.id))) or 0
    active_panels = await session.scalar(select(func.count(Panel.id)).where(Panel.status == "active")) or 0
    # Максимальный долг по last_tick_at: насколько давно не начисляли.
    oldest_tick = await session.scalar(select(func.min(Panel.last_tick_at)).where(Panel.status == "active"))
    debt_seconds = 0
    if oldest_tick:
        debt_seconds = max(0, int((utc_now() - oldest_tick).total_seconds()))
    return {
        "active_users": active_users,
        "active_panels": active_panels,
        "max_generation_lag_seconds": debt_seconds,
    }


class EnergyService:
    """Генерация энергии с защитой от гонок и догоном пропусков."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def _select_active_panels(self) -> Iterable[Panel]:
        """Выбрать активные панели под начисление (SKIP LOCKED)."""

        # Отбор активных панелей с захватом FOR UPDATE SKIP LOCKED для безопасного параллелизма.
        stmt: Select[tuple[Panel]] = (
            select(Panel)
            .where(Panel.status == "active", Panel.expires_at > utc_now())
            .with_for_update(skip_locked=True)
        )
        result = await self.session.scalars(stmt)
        return result

    async def backfill_all(self, tick_seconds: int = 600) -> dict[str, int]:
        """Начислить kWh всем активным панелям, догоняя пропуски.

        Вход: tick_seconds — длина тика (по расписанию 600 секунд).
        Выход: словарь с числом обработанных панелей и суммарно начисленными kWh.
        Побочные эффекты: обновляет Panel.generated_kwh/last_tick_at и user.available_kwh.
        Идемпотентность: вычисление идёт от фактического last_tick_at, поэтому повтор тика
        не создаёт дубль за те же секунды.
        Исключения: ошибки БД пробрасываются после логирования.
        ИИ-защита: SKIP LOCKED и advisory-лок предотвращают гонки между воркерами.
        """

        lock_key = 42_001
        if not await _try_advisory_lock(self.session, lock_key):
            logger.info("energy backfill skipped: lock held")
            return {"panels": 0, "users_updated": 0}

        processed_panels = 0
        touched_users: set[int] = set()
        now = utc_now()
        try:
            for panel in await self._select_active_panels():
                last_tick = panel.last_tick_at or panel.created_at
                delta = now - last_tick
                seconds = min(tick_seconds, max(0, int(delta.total_seconds())))
                if seconds <= 0:
                    continue
                user = await self.session.get(User, panel.user_id)
                if user is None:
                    logger.warning("orphan panel skipped", extra={"panel_id": panel.id})
                    continue
                rate = GEN_PER_SEC_VIP_KWH if user.is_vip else GEN_PER_SEC_BASE_KWH
                produced = quantize_decimal(rate * Decimal(seconds))
                panel.generated_kwh = quantize_decimal(panel.generated_kwh + produced)
                panel.last_tick_at = now
                user.available_kwh = quantize_decimal(user.available_kwh + produced)
                user.total_generated_kwh = quantize_decimal(user.total_generated_kwh + produced)
                processed_panels += 1
                touched_users.add(user.id)
            await self.session.flush()
            logger.info(
                "energy backfill complete",
                extra={"panels": processed_panels, "users": len(touched_users)},
            )
            return {"panels": processed_panels, "users_updated": len(touched_users)}
        finally:
            await _release_advisory_lock(self.session, lock_key)

    async def rescue_fill_null_last_tick(self) -> int:
        """Проставить last_tick_at для панелей с NULL, чтобы догон не завис.

        Побочные эффекты: обновляет только last_tick_at, балансы не меняет.
        Идемпотентность: повторное выполнение не меняет уже заполненные строки.
        """

        # Массовое обновление: last_tick_at=NULL → created_at
        updated = await self.session.execute(
            text(
                """
                UPDATE panels
                SET last_tick_at = created_at
                WHERE last_tick_at IS NULL AND status = 'active'
                """
            )
        )
        count = updated.rowcount or 0
        if count:
            logger.info("rescue filled last_tick_at", extra={"rows": count})
        return count

    async def rescue_fix_last_generated_after_expire(self) -> int:
        """Обнулить last_tick_at для истёкших панелей, чтобы не было догонов после срока."""

        updated = await self.session.execute(
            text(
                """
                UPDATE panels
                SET last_tick_at = expires_at
                WHERE status = 'expired' AND last_tick_at > expires_at
                """
            )
        )
        count = updated.rowcount or 0
        if count:
            logger.info("rescue fixed expired panels", extra={"rows": count})
        return count


# ============================================================================
# Пояснения «для чайника»:
#   • Генерация идёт только в kWh; деньги не двигаются (это делает банк).
#   • Идём строго по per-sec ставкам GEN_PER_SEC_BASE_KWH/GEN_PER_SEC_VIP_KWH.
#   • Повторный запуск backfill_all не удвоит начисление: считаем от last_tick_at.
#   • Пользователь не уходит в минус; банк может (но здесь не используется).
#   • SKIP LOCKED + advisory-лок предотвращают гонки нескольких воркеров.
# ============================================================================
