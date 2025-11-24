# -*- coding: utf-8 -*-
# backend/app/services/admin/admin_stats_service.py
# =============================================================================
# EFHC Bot — Статистика для админ-панели (монеты, панели, рефералы, магазин)
# -----------------------------------------------------------------------------
# Назначение:
#   • Даёт агрегированную статистику для дашборда админки:
#       - монеты EFHC (основной/бонусный баланс пользователей + Банк),
#       - энергия kWh (доступная и общая генерация),
#       - панели (активные/истёкшие + топ-пользователи),
#       - рефералки (кол-во, прямые бонусы, пороги),
#       - магазин (заказы, эквивалент EFHC, разбивка по товарам).
#
# Ключевые инварианты (канон):
#   • Никаких P2P-переводов — вся статистика по денежным потокам строится
#     ИСКЛЮЧИТЕЛЬНО по движениям Банк ↔ Пользователь (через efhc_transfers_log)
#     и по агрегированным балансам.
#   • Все EFHC храним раздельно:
#       users.main_balance       — основной баланс;
#       users.bonus_balance      — бонусный баланс;
#       bank_balances.efhc_balance — баланс Банка EFHC.
#   • Энергия:
#       users.available_kwh       — доступно к обмену;
#       users.total_generated_kwh — общая генерация (для рейтингов/достижений).
#   • Статистика только читает данные и НЕ меняет состояние.
#
# ИИ-защита:
#   • Все запросы обёрнуты в try/except — при частичной ошибке модуль
#     логирует проблему и возвращает «нулевые» безопасные значения, а не
#     падает, чтобы не ломать админку.
#   • В health_snapshot() дополнительно выявляем базовые аномалии:
#       - отрицательные балансы EFHC;
#       - отрицательные kWh;
#       - расхождение между «учтёнными EFHC» и условным total_efhc.
# =============================================================================

from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.engine import Result
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config_core import get_settings
from backend.app.core.logging_core import get_logger
from backend.app.core.utils_core import (
    quantize_decimal,
    format_decimal_str,
)

logger = get_logger(__name__)
S = get_settings()

SCHEMA_CORE: str = getattr(S, "DB_SCHEMA_CORE", "efhc_core") or "efhc_core"
SCHEMA_ADMIN: str = getattr(S, "DB_SCHEMA_ADMIN", "efhc_admin") or "efhc_admin"
SCHEMA_REF: str = getattr(S, "DB_SCHEMA_REFERRAL", "efhc_referral") or "efhc_referral"

# Центральный банк EFHC (telegram_id банка). Если не задан — считаем 0.
BANK_TG_ID: int = int(getattr(S, "BANK_TELEGRAM_ID", 0) or 0)


# =============================================================================
# DTO-модели для статистики
# =============================================================================

class CoinsStats(BaseModel):
    """Сводка по EFHC и kWh в системе."""
    total_user_main_efhc: str = Field(..., description="Суммарный основной баланс EFHC у всех пользователей")
    total_user_bonus_efhc: str = Field(..., description="Суммарный бонусный баланс EFHC у всех пользователей")
    total_bank_efhc: str = Field(..., description="Баланс EFHC Банка (центральный кошелёк)")
    total_available_kwh: str = Field(..., description="Сумма доступной энергии kWh (available_kwh)")
    total_generated_kwh: str = Field(..., description="Суммарная генерация kWh (total_generated_kwh)")
    total_efhc_in_system: str = Field(..., description="Итого EFHC в системе (пользователи+банк)")


class PanelsStats(BaseModel):
    """Статистика по панелям."""
    total_active_panels: int = Field(..., description="Количество активных панелей")
    total_inactive_panels: int = Field(..., description="Количество неактивных/истёкших панелей")
    top_users_by_panels: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Топ пользователей по количеству активных панелей",
    )


class ReferralsStats(BaseModel):
    """Статистика по рефералкам."""
    total_referrals: int = Field(..., description="Количество реферальных связей (ref_links)")
    total_direct_bonus_paid_efhc: str = Field(..., description="Сумма прямых реферальных бонусов (EFHC, бонусный баланс)")
    total_threshold_bonus_paid_efhc: str = Field(..., description="Сумма пороговых реферальных бонусов (EFHC, бонусный баланс)")
    thresholds_paid_counts: Dict[str, int] = Field(
        default_factory=dict,
        description="Сколько раз выплачен каждый порог (threshold → count)",
    )


class ShopStats(BaseModel):
    """Статистика магазина (по EFHC-эквиваленту)."""
    total_orders: int = Field(..., description="Количество оплаченных/доставленных заказов")
    total_amount_efhc_equiv: str = Field(..., description="Суммарный объём заказов в EFHC-эквиваленте")
    items_breakdown: Dict[str, int] = Field(
        default_factory=dict,
        description="Разбивка по кодам товаров: item_code → количество",
    )


class SystemStats(BaseModel):
    """Комплексная сводка для дашборда."""
    coins: CoinsStats
    panels: PanelsStats
    referrals: ReferralsStats
    shop: ShopStats


class StatsHealthSnapshot(BaseModel):
    """
    Лёгкая самодиагностика статистики:
      • признаки неконсистентности/аномалий;
      • агрегированные показатели, полезные для мониторинга.
    """
    negative_user_main_balances: int
    negative_user_bonus_balances: int
    negative_user_available_kwh: int
    total_efhc_reported: str
    total_efhc_recomputed: str
    mismatch_efhc_flag: bool


# =============================================================================
# AdminStatsService — сервис статистики
# =============================================================================

class AdminStatsService:
    """
    Высокоуровневый сервис статистики (READ-ONLY).
    Используется фасадом AdminService и админ-роутами.
    """

    # -------------------------------------------------------------------------
    # Монеты EFHC и энергия kWh
    # -------------------------------------------------------------------------

    @staticmethod
    async def get_coins_stats(db: AsyncSession) -> CoinsStats:
        """
        Возвращает общую сводку по EFHC (основной+бонусный+банк) и kWh (доступной и общей).

        Таблицы/поля:
          • {SCHEMA_CORE}.users:
                main_balance          NUMERIC
                bonus_balance         NUMERIC
                available_kwh         NUMERIC
                total_generated_kwh   NUMERIC
          • {SCHEMA_ADMIN}.bank_balances:
                telegram_id           BIGINT (BANK_TG_ID)
                efhc_balance          NUMERIC
        """
        total_main = Decimal("0")
        total_bonus = Decimal("0")
        total_avail_kwh = Decimal("0")
        total_gen_kwh = Decimal("0")
        total_bank = Decimal("0")

        # 1) Суммы по пользователям
        try:
            r_users: Result = await db.execute(
                text(
                    f"""
                    SELECT
                        COALESCE(SUM(main_balance), 0)          AS total_main,
                        COALESCE(SUM(bonus_balance), 0)         AS total_bonus,
                        COALESCE(SUM(available_kwh), 0)         AS total_avail,
                        COALESCE(SUM(total_generated_kwh), 0)   AS total_gen
                    FROM {SCHEMA_CORE}.users
                    """
                )
            )
            row = r_users.fetchone()
            if row:
                total_main = quantize_decimal(Decimal(str(row.total_main or "0")), 8, "DOWN")
                total_bonus = quantize_decimal(Decimal(str(row.total_bonus or "0")), 8, "DOWN")
                total_avail_kwh = quantize_decimal(Decimal(str(row.total_avail or "0")), 8, "DOWN")
                total_gen_kwh = quantize_decimal(Decimal(str(row.total_gen or "0")), 8, "DOWN")
        except Exception as e:
            logger.error("AdminStatsService.get_coins_stats: ошибка при агрегации users: %s", e)

        # 2) Баланс Банка EFHC (по телеграм-ID банка)
        try:
            r_bank: Result = await db.execute(
                text(
                    f"""
                    SELECT COALESCE(efhc_balance, 0) AS bank
                    FROM {SCHEMA_ADMIN}.bank_balances
                    WHERE telegram_id = :tid
                    LIMIT 1
                    """
                ),
                {"tid": BANK_TG_ID},
            )
            b_row = r_bank.fetchone()
            if b_row:
                total_bank = quantize_decimal(Decimal(str(b_row.bank or "0")), 8, "DOWN")
        except Exception as e:
            logger.error("AdminStatsService.get_coins_stats: ошибка при чтении bank_balances: %s", e)

        total_efhc = quantize_decimal(total_main + total_bonus + total_bank, 8, "DOWN")

        return CoinsStats(
            total_user_main_efhc=format_decimal_str(total_main, 8),
            total_user_bonus_efhc=format_decimal_str(total_bonus, 8),
            total_bank_efhc=format_decimal_str(total_bank, 8),
            total_available_kwh=format_decimal_str(total_avail_kwh, 8),
            total_generated_kwh=format_decimal_str(total_gen_kwh, 8),
            total_efhc_in_system=format_decimal_str(total_efhc, 8),
        )

    # -------------------------------------------------------------------------
    # Панели: активные/истёкшие и топ-пользователи
    # -------------------------------------------------------------------------

    @staticmethod
    async def get_panels_stats(db: AsyncSession) -> PanelsStats:
        """
        Возвращает статистику по панелям:
          • общее количество активных и неактивных/истёкших панелей;
          • топ-20 пользователей по количеству активных панелей.
        """
        total_active = 0
        total_inactive = 0
        top: List[Dict[str, Any]] = []

        # 1) Активные/неактивные панели
        try:
            r1: Result = await db.execute(
                text(
                    f"""
                    SELECT
                        SUM(CASE WHEN is_active THEN 1 ELSE 0 END) AS active_cnt,
                        SUM(CASE WHEN is_active THEN 0 ELSE 1 END) AS inactive_cnt
                    FROM {SCHEMA_CORE}.panels
                    """
                )
            )
            x = r1.fetchone()
            if x:
                total_active = int(x.active_cnt or 0)
                total_inactive = int(x.inactive_cnt or 0)
        except Exception as e:
            logger.error("AdminStatsService.get_panels_stats: ошибка агрегации по панелям: %s", e)

        # 2) Топ пользователей по активным панелям
        try:
            r2: Result = await db.execute(
                text(
                    f"""
                    SELECT user_id, COUNT(1) AS c
                    FROM {SCHEMA_CORE}.panels
                    WHERE is_active = TRUE
                    GROUP BY user_id
                    ORDER BY c DESC
                    LIMIT 20
                    """
                )
            )
            for row in r2.fetchall():
                top.append(
                    {
                        "user_id": int(row.user_id),
                        "active_panels": int(row.c),
                    }
                )
        except Exception as e:
            logger.error("AdminStatsService.get_panels_stats: ошибка выборки top_users_by_panels: %s", e)

        return PanelsStats(
            total_active_panels=total_active,
            total_inactive_panels=total_inactive,
            top_users_by_panels=top,
        )

    # -------------------------------------------------------------------------
    # Реферальная статистика: количества, прямые и пороговые бонусы
    # -------------------------------------------------------------------------

    @staticmethod
    async def get_referrals_stats(db: AsyncSession) -> ReferralsStats:
        """
        Статистика рефералок:
          • всего реферальных связей (ref_links);
          • сумма прямых бонусов (reason='ref_direct_bonus');
          • сумма пороговых бонусов (reason='ref_threshold_bonus');
          • сколько раз выплачен каждый порог (threshold → count).
        """
        total_refs = 0
        total_direct = Decimal("0")
        total_threshold = Decimal("0")
        thresholds_paid: Dict[str, int] = {}

        # 1) Общее число рефералов
        try:
            r1: Result = await db.execute(
                text(
                    f"""
                    SELECT COUNT(1) AS cnt
                    FROM {SCHEMA_REF}.ref_links
                    """
                )
            )
            row = r1.fetchone()
            if row:
                total_refs = int(row.cnt or 0)
        except Exception as e:
            logger.error("AdminStatsService.get_referrals_stats: ошибка COUNT(ref_links): %s", e)

        # 2) Прямые бонусы (reason='ref_direct_bonus')
        try:
            r2: Result = await db.execute(
                text(
                    f"""
                    SELECT COALESCE(SUM(amount), 0) AS total_direct
                    FROM {SCHEMA_CORE}.efhc_transfers_log
                    WHERE reason = 'ref_direct_bonus'
                    """
                )
            )
            row2 = r2.fetchone()
            if row2:
                total_direct = quantize_decimal(Decimal(str(row2.total_direct or "0")), 8, "DOWN")
        except Exception as e:
            logger.error("AdminStatsService.get_referrals_stats: ошибка SUM(ref_direct_bonus): %s", e)

        # 3) Пороговые бонусы (reason='ref_threshold_bonus')
        try:
            r3: Result = await db.execute(
                text(
                    f"""
                    SELECT COALESCE(SUM(amount), 0) AS total_threshold
                    FROM {SCHEMA_CORE}.efhc_transfers_log
                    WHERE reason = 'ref_threshold_bonus'
                    """
                )
            )
            row3 = r3.fetchone()
            if row3:
                total_threshold = quantize_decimal(Decimal(str(row3.total_threshold or "0")), 8, "DOWN")
        except Exception as e:
            logger.error("AdminStatsService.get_referrals_stats: ошибка SUM(ref_threshold_bonus): %s", e)

        # 4) Сколько раз выплачен каждый порог (по ref_threshold_rewards)
        try:
            r4: Result = await db.execute(
                text(
                    f"""
                    SELECT threshold, COUNT(1) AS cnt
                    FROM {SCHEMA_REF}.ref_threshold_rewards
                    GROUP BY threshold
                    ORDER BY threshold ASC
                    """
                )
            )
            for row in r4.fetchall():
                thr = str(int(row.threshold))
                thresholds_paid[thr] = int(row.cnt or 0)
        except Exception as e:
            logger.error("AdminStatsService.get_referrals_stats: ошибка агрегации ref_threshold_rewards: %s", e)

        return ReferralsStats(
            total_referrals=total_refs,
            total_direct_bonus_paid_efhc=format_decimal_str(total_direct, 8),
            total_threshold_bonus_paid_efhc=format_decimal_str(total_threshold, 8),
            thresholds_paid_counts=thresholds_paid,
        )

    # -------------------------------------------------------------------------
    # Магазин: заказы, эквивалент EFHC, разбивка по товарам
    # -------------------------------------------------------------------------

    @staticmethod
    async def get_shop_stats(db: AsyncSession) -> ShopStats:
        """
        Статистика магазина:
          • количество заказов со статусом PAID/DELIVERED;
          • суммарный EFHC-эквивалент;
          • разбивка по кодам товаров.
        """
        total_orders = 0
        total_efhc_equiv = Decimal("0")
        breakdown: Dict[str, int] = {}

        # 1) Общая статистика
        try:
            r1: Result = await db.execute(
                text(
                    f"""
                    SELECT
                        COUNT(1) AS cnt,
                        COALESCE(SUM(efhc_equiv_amount), 0) AS total_efhc
                    FROM {SCHEMA_ADMIN}.shop_orders
                    WHERE status IN ('PAID', 'DELIVERED')
                    """
                )
            )
            row = r1.fetchone()
            if row:
                total_orders = int(row.cnt or 0)
                total_efhc_equiv = quantize_decimal(Decimal(str(row.total_efhc or "0")), 8, "DOWN")
        except Exception as e:
            logger.error("AdminStatsService.get_shop_stats: ошибка агрегации shop_orders: %s", e)

        # 2) Разбивка по товарам
        try:
            r2: Result = await db.execute(
                text(
                    f"""
                    SELECT item_code, COUNT(1) AS cnt
                    FROM {SCHEMA_ADMIN}.shop_orders
                    WHERE status IN ('PAID', 'DELIVERED')
                    GROUP BY item_code
                    ORDER BY cnt DESC
                    """
                )
            )
            for row in r2.fetchall():
                breakdown[str(row.item_code)] = int(row.cnt or 0)
        except Exception as e:
            logger.error("AdminStatsService.get_shop_stats: ошибка разбивки по item_code: %s", e)

        return ShopStats(
            total_orders=total_orders,
            total_amount_efhc_equiv=format_decimal_str(total_efhc_equiv, 8),
            items_breakdown=breakdown,
        )

    # -------------------------------------------------------------------------
    # Комплексная сводка
    # -------------------------------------------------------------------------

    @staticmethod
    async def get_system_stats(db: AsyncSession) -> SystemStats:
        """
        Общая сводка для дашборда админки:
          • Монеты (пользователи/банк/бонус/всего) + энергия kWh
          • Панели (активные/неактивные, топ-20 пользователей)
          • Рефералки (всего, прямые бонусы, пороговые бонусы)
          • Магазин (заказы, EFHC-эквивалент, разбивка по товарам)
        """
        coins = await AdminStatsService.get_coins_stats(db)
        panels = await AdminStatsService.get_panels_stats(db)
        refs = await AdminStatsService.get_referrals_stats(db)
        shop = await AdminStatsService.get_shop_stats(db)
        return SystemStats(coins=coins, panels=panels, referrals=refs, shop=shop)

    # -------------------------------------------------------------------------
    # Лёгкий health-чек статистики (ИИ-самодиагностика)
    # -------------------------------------------------------------------------

    @staticmethod
    async def health_snapshot(db: AsyncSession) -> StatsHealthSnapshot:
        """
        Проводит лёгкую самодиагностику статистики:
          • количество пользователей с отрицательными EFHC/kWh;
          • сравнение total_efhc_in_system из get_coins_stats с грубо
            пересчитанной суммой (для выявления расхождений).
        """
        negatives_main = 0
        negatives_bonus = 0
        negatives_kwh = 0

        # Проверяем отрицательные значения на users
        try:
            r1: Result = await db.execute(
                text(
                    f"""
                    SELECT
                        SUM(CASE WHEN main_balance < 0 THEN 1 ELSE 0 END)      AS neg_main,
                        SUM(CASE WHEN bonus_balance < 0 THEN 1 ELSE 0 END)     AS neg_bonus,
                        SUM(CASE WHEN available_kwh < 0 THEN 1 ELSE 0 END)     AS neg_kwh
                    FROM {SCHEMA_CORE}.users
                    """
                )
            )
            row = r1.fetchone()
            if row:
                negatives_main = int(row.neg_main or 0)
                negatives_bonus = int(row.neg_bonus or 0)
                negatives_kwh = int(row.neg_kwh or 0)
        except Exception as e:
            logger.error("AdminStatsService.health_snapshot: ошибка проверки отрицательных значений: %s", e)

        # Сравнение total_efhc_in_system по двум методам:
        coins = await AdminStatsService.get_coins_stats(db)

        # Пересчёт: users.main + users.bonus + bank_balances
        recomputed = Decimal("0")
        try:
            r2: Result = await db.execute(
                text(
                    f"""
                    SELECT
                        COALESCE(SUM(main_balance), 0)   AS total_main,
                        COALESCE(SUM(bonus_balance), 0)  AS total_bonus
                    FROM {SCHEMA_CORE}.users
                    """
                )
            )
            row2 = r2.fetchone()
            if row2:
                m = quantize_decimal(Decimal(str(row2.total_main or "0")), 8, "DOWN")
                b = quantize_decimal(Decimal(str(row2.total_bonus or "0")), 8, "DOWN")
            else:
                m = Decimal("0")
                b = Decimal("0")

            r3: Result = await db.execute(
                text(
                    f"""
                    SELECT COALESCE(efhc_balance, 0) AS bank
                    FROM {SCHEMA_ADMIN}.bank_balances
                    WHERE telegram_id = :tid
                    LIMIT 1
                    """
                ),
                {"tid": BANK_TG_ID},
            )
            row3 = r3.fetchone()
            if row3:
                bank_val = quantize_decimal(Decimal(str(row3.bank or "0")), 8, "DOWN")
            else:
                bank_val = Decimal("0")

            recomputed = quantize_decimal(m + b + bank_val, 8, "DOWN")
        except Exception as e:
            logger.error("AdminStatsService.health_snapshot: ошибка пересчёта EFHC: %s", e)

        reported = Decimal(coins.total_efhc_in_system)
        mismatch_flag = (reported != recomputed)

        return StatsHealthSnapshot(
            negative_user_main_balances=negatives_main,
            negative_user_bonus_balances=negatives_bonus,
            negative_user_available_kwh=negatives_kwh,
            total_efhc_reported=format_decimal_str(reported, 8),
            total_efhc_recomputed=format_decimal_str(recomputed, 8),
            mismatch_efhc_flag=bool(mismatch_flag),
        )


__all__ = [
    "CoinsStats",
    "PanelsStats",
    "ReferralsStats",
    "ShopStats",
    "SystemStats",
    "StatsHealthSnapshot",
    "AdminStatsService",
]

