# -*- coding: utf-8 -*-
# backend/app/services/admin/admin_facade.py
# =============================================================================
# EFHC Bot — AdminService (фасад админ-подсистемы)
# -----------------------------------------------------------------------------
# Назначение:
#   • Дать ЕДИНЫЙ высокий уровень API для админ-роутов и фоновых воркеров.
#   • Инкапсулировать всю внутреннюю модульную структуру admin-модуля.
#   • Дополнительно защищать канон проекта на уровне публичного интерфейса:
#       - НЕТ P2P переводов «пользователь ↔ пользователь»;
#       - ВСЕ бонусы → только бонусный баланс;
#       - ВСЕ денежные операции идемпотентны (idempotency_key на нижнем уровне);
#       - ВСЕ транзакции только «Банк ↔ Пользователь».
#
# ВНИМАНИЕ (жёсткий канон):
#   1) Начальный баланс банка: 5 000 000 EFHC. Любые корректировки только
#      через банк-сервис (минт/бёрн/ручные транзакции «банк ↔ пользователь»).
#   2) Внутренние переводы между пользователями (user → user) ЗАПРЕЩЕНЫ.
#      В этом фасаде нет и не будет методов, которые позволяли бы делать P2P.
#   3) Все бонусные начисления (реферальные, задания, лотереи и др.) идут только
#      на бонусный счет. Фасад не даёт доступ к «бонус→regular» конверсиям.
#   4) Все операции, двигающие EFHC (включая ручные действия админа), обязаны
#      использовать идемпотентные сервисы нижнего уровня с явным ключом.
#
# Как использовать:
#   from backend.app.services.admin.admin_facade import AdminService
#
#   admin = await AdminService.resolve_admin(db, telegram_id)
#   stats = await AdminService.get_system_stats(db)
#   lotteries = await AdminService.list_lotteries(db, pg)
# =============================================================================

from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, List, Optional

from sqlalchemy.ext.asyncio import AsyncSession

# Базовые сущности RBAC/логи/настройки/нотификации
from backend.app.services.admin.admin_rbac import (
    AdminAuthError,
    AdminRole,
    AdminUser,
    RBAC,
)
from backend.app.services.admin.admin_logging import (
    AdminLog,
    AdminLogger,
    Pagination,
)
from backend.app.services.admin.admin_settings import (
    ALLOWED_SETTINGS,
    SettingsService,
    SystemSetting,
)
from backend.app.services.admin.admin_notifications import (
    AdminNotification,
    AdminNotifier,
)

# Банк EFHC
from backend.app.services.admin.admin_bank_service import (
    AdminBankService,
    MintBurnRequest,
    ManualBankTransferRequest,
    BankBalanceDTO,
)

# Лотереи / билеты / призы
from backend.app.services.admin.admin_lotteries_service import (
    AdminLotteriesService,
    CreateLotteryRequest,
    UpdateLotteryRequest,
    LotteryInfo,
    WinnerTicket,
    LotteryStatusSummary,
)

# Панели пользователей
from backend.app.services.admin.admin_panels_service import (
    AdminPanelsService,
    PanelToggleRequest,
)

# Реферальные бонусы
from backend.app.services.admin.admin_referral_service import (
    AdminReferralService,
)

# Кошельки пользователей
from backend.app.services.admin.admin_wallets_service import (
    AdminWalletsService,
)

# Статистика
from backend.app.services.admin.admin_stats_service import (
    AdminStatsService,
    CoinsStats,
    PanelsStats,
    ReferralsStats,
    ShopStats,
    SystemStats,
)

# Пользователи / прогресс / отладка
from backend.app.services.admin.admin_users_service import (
    AdminUsersService,
    UserProgressSnapshot,
)

# Вывод EFHC / призовые заявки / бонус-на-вывод
from backend.app.services.admin.admin_withdrawals_service import (
    AdminWithdrawalsService,
    WithdrawRequestAdminDTO,
    WithdrawFilters,
    BonusPayoutFilters,
    PrizeClaimFilters,
)

# =============================================================================
# AdminService — тонкий фасад
# =============================================================================


class AdminService:
    """
    Высокоуровневый фасад над всеми админ-подсистемами.

    ВАЖНО:
      • Здесь НЕТ бизнес-логики по деньгам — только делегация.
      • Канон безопасности обеспечивается тем, ЧТО именно мы экспортируем:
          - нет методов P2P-переводов;
          - нет методов «прямого» изменения user.balance в обход банка;
          - нет обратных обменов EFHC↔kWh.
    """

    # --------------------------------------------------------------------- #
    # 0. RBAC / Аутентификация админа                                      #
    # --------------------------------------------------------------------- #

    @staticmethod
    async def resolve_admin(db: AsyncSession, telegram_id: int) -> AdminUser:
        """
        Возвращает AdminUser по telegram_id или бросает AdminAuthError.

        Используется роутами для авторизации входа в админ-панель.
        """
        return await RBAC.resolve_admin(db, telegram_id=telegram_id)

    @staticmethod
    def require_role(admin: AdminUser, minimal: str) -> None:
        """
        Проверка, что роль admin «не ниже», чем требуемая minimal.
        """
        RBAC.require_role(admin, minimal)

    # --------------------------------------------------------------------- #
    # 1. ЛОГИ АДМИНОВ                                                       #
    # --------------------------------------------------------------------- #

    @staticmethod
    async def list_admin_logs(
        db: AsyncSession,
        pg: Pagination,
        admin_id: Optional[int] = None,
        action: Optional[str] = None,
        entity: Optional[str] = None,
    ) -> List[AdminLog]:
        """
        Возвращает список логов действий админов (для аудита).
        """
        return await AdminLogger.list(db, pg=pg, admin_id=admin_id, action=action, entity=entity)

    # --------------------------------------------------------------------- #
    # 2. СИСТЕМНЫЕ НАСТРОЙКИ АДМИНКИ (+ ежедневные снапшоты / откат)       #
    # --------------------------------------------------------------------- #

    @staticmethod
    async def get_setting(db: AsyncSession, key: str) -> Optional[SystemSetting]:
        """Прочитать одну настройку (или None, если не существует)."""
        return await SettingsService.get(db, key)

    @staticmethod
    async def mget_settings(db: AsyncSession, keys: List[str]) -> List[SystemSetting]:
        """Пакетное чтение нескольких настроек."""
        return await SettingsService.mget(db, keys)

    @staticmethod
    async def set_setting(db: AsyncSession, key: str, value: str, admin: AdminUser) -> None:
        """
        Установить/обновить одну настройку.

        ИИ-страховка:
          • key проверяется на принадлежность ALLOWED_SETTINGS.
          • Значения проходят валидацию (числа/пороги/строки).
        """
        return await SettingsService.set(db, key, value, admin_id=admin.id)

    @staticmethod
    async def mset_settings(db: AsyncSession, items: Dict[str, str], admin: AdminUser) -> None:
        """
        Пакетное обновление настроек (каждая — через set_setting).
        """
        return await SettingsService.mset(db, items, admin_id=admin.id)

    @staticmethod
    async def snapshot_settings_daily(db: AsyncSession) -> None:
        """
        Сохранение слепка настроек (для отката). Вызывается планировщиком раз в сутки.
        """
        await SettingsService.snapshot_all(db)

    @staticmethod
    async def restore_settings_from_snapshot(db: AsyncSession, snapshot_id: int, admin: AdminUser) -> None:
        """
        Откат настроек к выбранному снапшоту (ИИ-защита от ошибочных изменений).
        """
        RBAC.require_role(admin, AdminRole.SUPERADMIN)
        await SettingsService.restore_from_snapshot(db, snapshot_id=snapshot_id, admin_id=admin.id)

    # --------------------------------------------------------------------- #
    # 3. БАНК EFHC: минт/бёрн/ручные транзакции Банк↔Пользователь          #
    # --------------------------------------------------------------------- #

    @staticmethod
    async def get_bank_balance(db: AsyncSession) -> BankBalanceDTO:
        """
        Возвращает агрегированный баланс банка EFHC (для дашборда).
        """
        return await AdminBankService.get_bank_balance(db)

    @staticmethod
    async def mint_efhc(db: AsyncSession, req: MintBurnRequest, admin: AdminUser) -> Dict[str, Any]:
        """
        Минт EFHC в банк. НЕ начисляет пользователям напрямую.
        """
        return await AdminBankService.mint_efhc(db, req=req, admin=admin)

    @staticmethod
    async def burn_efhc(db: AsyncSession, req: MintBurnRequest, admin: AdminUser) -> Dict[str, Any]:
        """
        Сжигание EFHC из банка (требуется достаточный остаток).
        """
        return await AdminBankService.burn_efhc(db, req=req, admin=admin)

    @staticmethod
    async def manual_bank_to_user(
        db: AsyncSession,
        req: ManualBankTransferRequest,
        admin: AdminUser,
    ) -> Dict[str, Any]:
        """
        Ручная транзакция «Банк → Пользователь».

        ИИ-защита:
          • нет возможности указать второго пользователя (P2P);
          • идемпотентность контролируется на уровне AdminBankService;
          • лог транзакции записывается и в банковские логи, и в admin_logs.
        """
        return await AdminBankService.manual_bank_to_user(db, req=req, admin=admin)

    @staticmethod
    async def manual_user_to_bank(
        db: AsyncSession,
        req: ManualBankTransferRequest,
        admin: AdminUser,
    ) -> Dict[str, Any]:
        """
        Ручная транзакция «Пользователь → Банк» (например, исправление ошибочного начисления).
        """
        return await AdminBankService.manual_user_to_bank(db, req=req, admin=admin)

    # --------------------------------------------------------------------- #
    # 4. ЛОТЕРЕИ / БИЛЕТЫ / ПРИЗЫ                                          #
    # --------------------------------------------------------------------- #

    @staticmethod
    async def create_lottery(db: AsyncSession, req: CreateLotteryRequest, admin: AdminUser) -> int:
        """
        Создание лотереи (универсальный конструктор).

        Параметры (через req) могут полностью менять:
          • цену билета,
          • лимиты участников/билетов,
          • тип/размер приза (бонус EFHC или NFT VIP).
        """
        return await AdminLotteriesService.create_lottery(db, req=req, admin=admin)

    @staticmethod
    async def update_lottery(db: AsyncSession, lottery_id: int, req: UpdateLotteryRequest, admin: AdminUser) -> None:
        """
        Частичное обновление лотереи:
          • изменение стоимости билета, лимитов, статуса (ACTIVE/PAUSED/FINISHED).
        """
        return await AdminLotteriesService.update_lottery(db, lottery_id=lottery_id, req=req, admin=admin)

    @staticmethod
    async def list_lotteries(db: AsyncSession, pg: Pagination) -> List[LotteryInfo]:
        """Список лотерей с краткой информацией для админ-панели."""
        return await AdminLotteriesService.list_lotteries(db, pg=pg)

    @staticmethod
    async def draw_lottery(db: AsyncSession, lottery_id: int, admin: AdminUser) -> Optional[WinnerTicket]:
        """
        Розыгрыш лотереи:
          • выбирается один победитель;
          • создаются идемпотентные заявки на приз (bonus_awards / prize_claims);
          • сама лотерея переводится в FINISHED.
        """
        return await AdminLotteriesService.draw_lottery(db, lottery_id=lottery_id, admin=admin)

    @staticmethod
    async def lottery_status_summary(db: AsyncSession) -> LotteryStatusSummary:
        """
        Краткая сводка по лотереям (активные, на паузе, завершённые, автоперезапуск).
        """
        return await AdminLotteriesService.get_status_summary(db)

    @staticmethod
    async def fix_lottery_tickets(
        db: AsyncSession,
        lottery_id: int,
        *,
        user_id: int,
        new_tickets_count: int,
        admin: AdminUser,
    ) -> Dict[str, Any]:
        """
        Ручная корректировка количества билетов пользователя в лотерее.

        Используется, если из-за сбоев идемпотентности покупки билетов возник
        рассинхрон. Все коррекции логируются.
        """
        return await AdminLotteriesService.fix_user_tickets(
            db,
            lottery_id=lottery_id,
            user_id=user_id,
            new_tickets_count=new_tickets_count,
            admin=admin,
        )

    # --------------------------------------------------------------------- #
    # 5. ПАНЕЛИ ПОЛЬЗОВАТЕЛЕЙ                                              #
    # --------------------------------------------------------------------- #

    @staticmethod
    async def toggle_user_panel(
        db: AsyncSession,
        req: PanelToggleRequest,
        admin: AdminUser,
    ) -> Dict[str, Any]:
        """
        Включение/выключение панелей пользователя (ручная корректировка).
        """
        return await AdminPanelsService.toggle_user_panel(db, req=req, admin=admin)

    # --------------------------------------------------------------------- #
    # 6. РЕФЕРАЛЬНАЯ СИСТЕМА (ВСЕГДА БОНУСНЫЙ СЧЁТ)                         #
    # --------------------------------------------------------------------- #

    @staticmethod
    async def award_referral_on_first_panel(
        db: AsyncSession,
        referrer_id: int,
        invitee_id: int,
    ) -> None:
        """
        0.1 EFHC бонусом за первую панель реферала (идемпотентно).
        """
        await AdminReferralService.award_referral_on_first_panel(db, referrer_id=referrer_id, invitee_id=invitee_id)

    @staticmethod
    async def award_referral_thresholds(db: AsyncSession, referrer_id: int) -> List[int]:
        """
        Проверяет и выплачивает пороговые награды за рефералов (10/100/1000/...),
        все выплаты — на бонусный счёт, строго идемпотентно по порогам.
        """
        return await AdminReferralService.award_referral_thresholds(db, referrer_id=referrer_id)

    # --------------------------------------------------------------------- #
    # 7. КОШЕЛЬКИ ПОЛЬЗОВАТЕЛЕЙ                                            #
    # --------------------------------------------------------------------- #

    @staticmethod
    async def list_user_wallets(db: AsyncSession, user_id: int) -> List[Dict[str, Any]]:
        """
        Возвращает список привязанных кошельков пользователя.

        Используется для:
          • выдачи NFT VIP,
          • проверок покупок,
          • проверок адресов при выводе.
        """
        return await AdminWalletsService.list_user_wallets(db, user_id=user_id)

    @staticmethod
    async def bind_user_wallet(
        db: AsyncSession,
        user_id: int,
        chain: str,
        address: str,
        *,
        set_primary: bool = True,
        admin: Optional[AdminUser] = None,
    ) -> int:
        """
        Привязывает кошелек к пользователю, опционально делая его основным.
        """
        return await AdminWalletsService.bind_user_wallet(
            db,
            user_id=user_id,
            chain=chain,
            address=address,
            set_primary=set_primary,
            admin=admin,
        )

    # --------------------------------------------------------------------- #
    # 8. СТАТИСТИКА ДЛЯ ДАШБОРДА                                           #
    # --------------------------------------------------------------------- #

    @staticmethod
    async def get_coins_stats(db: AsyncSession) -> CoinsStats:
        return await AdminStatsService.get_coins_stats(db)

    @staticmethod
    async def get_panels_stats(db: AsyncSession) -> PanelsStats:
        return await AdminStatsService.get_panels_stats(db)

    @staticmethod
    async def get_referrals_stats(db: AsyncSession) -> ReferralsStats:
        return await AdminStatsService.get_referrals_stats(db)

    @staticmethod
    async def get_shop_stats(db: AsyncSession) -> ShopStats:
        return await AdminStatsService.get_shop_stats(db)

    @staticmethod
    async def get_system_stats(db: AsyncSession) -> SystemStats:
        return await AdminStatsService.get_system_stats(db)

    # --------------------------------------------------------------------- #
    # 9. ПОЛЬЗОВАТЕЛИ / БАЛАНСЫ / ПРОГРЕСС / ОТЛАДКА                       #
    # --------------------------------------------------------------------- #

    @staticmethod
    async def get_user_balances(db: AsyncSession, user_id: int) -> Dict[str, str]:
        """
        Текущие балансы пользователя (EFHC обычные, EFHC бонусные, kWh).

        ВНИМАНИЕ:
          • Это только чтение; любые изменения балансов идут через банк-сервис.
        """
        return await AdminUsersService.get_user_balances(db, user_id=user_id)

    @staticmethod
    async def debug_user_progress_by_telegram(
        db: AsyncSession,
        telegram_id: int,
    ) -> UserProgressSnapshot:
        """
        Открывает «окно игры» пользователя по Telegram ID:
          • панели, VIP-статус,
          • энергия (available_kwh, total_generated_kwh),
          • уровни/достижения,
          • последние операции.

        Используется для отладки сбоев и ручных исправлений.
        """
        return await AdminUsersService.debug_user_progress_by_telegram(db, telegram_id=telegram_id)

    @staticmethod
    async def credit_bonus_efhc(
        db: AsyncSession,
        user_id: int,
        amount: Decimal,
        *,
        reason: str = "ADMIN",
        admin: Optional[AdminUser] = None,
    ) -> None:
        """
        Ручное бонусное начисление EFHC пользователю.

        ИИ-ограничения:
          • только бонусный счёт;
          • операции считаются «банк → пользователь» и логируются;
          • идемпотентность обеспечивается на уровне AdminUsersService/BankService.
        """
        await AdminUsersService.credit_bonus_efhc(db, user_id=user_id, amount=amount, reason=reason, admin=admin)

    @staticmethod
    async def credit_regular_efhc(
        db: AsyncSession,
        user_id: int,
        amount: Decimal,
        *,
        reason: str = "ADMIN",
        admin: Optional[AdminUser] = None,
    ) -> None:
        """
        Ручное начисление обычных EFHC пользователю (редкие корректировки).

        Разрешено только администраторам с соответствующей ролью.
        """
        await AdminUsersService.credit_regular_efhc(db, user_id=user_id, amount=amount, reason=reason, admin=admin)

    # --------------------------------------------------------------------- #
    # 10. ЗАЯВКИ НА ВЫВОД EFHC / БОНУСЫ НА ВЫВОД / NFT ПО ЗАЯВКАМ          #
    # --------------------------------------------------------------------- #

    @staticmethod
    async def list_withdraw_requests(
        db: AsyncSession,
        filters: WithdrawFilters,
    ) -> List[WithdrawRequestAdminDTO]:
        """
        Список заявок на вывод EFHC (для обработки админами).
        """
        return await AdminWithdrawalsService.list_withdraw_requests(db, filters=filters)

    @staticmethod
    async def admin_approve_withdraw(
        db: AsyncSession,
        request_id: int,
        admin: AdminUser,
    ) -> WithdrawRequestAdminDTO:
        """
        Админ утвердил вывод (внешняя транзакция будет выполнена вручную/интеграцией).
        """
        return await AdminWithdrawalsService.admin_approve_withdraw(db, request_id=request_id, admin=admin)

    @staticmethod
    async def admin_reject_withdraw(
        db: AsyncSession,
        request_id: int,
        admin: AdminUser,
        reason: str,
    ) -> WithdrawRequestAdminDTO:
        """
        Админ отклонил вывод:
          • выполняется идемпотентный рефанд EFHC пользователю из Банка;
          • заявка помечается как REJECTED.
        """
        return await AdminWithdrawalsService.admin_reject_withdraw(
            db,
            request_id=request_id,
            admin=admin,
            reason=reason,
        )

    @staticmethod
    async def admin_mark_withdraw_paid(
        db: AsyncSession,
        request_id: int,
        admin: AdminUser,
        *,
        payout_ref: Optional[str] = None,
    ) -> WithdrawRequestAdminDTO:
        """
        Админ помечает заявку как PAID после фактической внешней выплаты.

        Денежных движений внутри бота нет:
          • EFHC уже были переведены в Банк на этапе создания заявки.
        """
        return await AdminWithdrawalsService.admin_mark_paid(
            db,
            request_id=request_id,
            admin=admin,
            payout_ref=payout_ref,
        )

    @staticmethod
    async def list_bonus_payouts(
        db: AsyncSession,
        filters: BonusPayoutFilters,
    ) -> List[Dict[str, Any]]:
        """
        Список бонусных начислений «на вывод» (например, из лотерей/заданий),
        которые ожидают подтверждения/обработки.
        """
        return await AdminWithdrawalsService.list_bonus_payouts(db, filters=filters)

    @staticmethod
    async def list_prize_claims(
        db: AsyncSession,
        filters: PrizeClaimFilters,
    ) -> List[Dict[str, Any]]:
        """
        Список заявок на выдачу призов (NFT VIP и др.) по магазинам/лотереям.
        """
        return await AdminWithdrawalsService.list_prize_claims(db, filters=filters)

    @staticmethod
    async def mark_prize_claim_done(
        db: AsyncSession,
        claim_id: int,
        admin: AdminUser,
        tx_hash: Optional[str] = None,
    ) -> None:
        """
        Пометить заявку на приз как выполненную (NFT/внешний актив выдан).
        """
        await AdminWithdrawalsService.mark_prize_claim_done(db, claim_id=claim_id, admin=admin, tx_hash=tx_hash)

    @staticmethod
    async def reject_prize_claim(
        db: AsyncSession,
        claim_id: int,
        admin: AdminUser,
        reason: str,
    ) -> None:
        """
        Отклонить заявку на приз с указанием причины.
        """
        await AdminWithdrawalsService.reject_prize_claim(db, claim_id=claim_id, admin=admin, reason=reason)


# =============================================================================
# Публичный API модуля                                                      #
# =============================================================================

__all__ = [
    "AdminService",
    # Базовые сущности для удобства использования в роутерах:
    "AdminUser",
    "AdminRole",
    "AdminAuthError",
    "AdminLog",
    "Pagination",
    "SystemSetting",
    "ALLOWED_SETTINGS",
    "AdminNotification",
    "CreateLotteryRequest",
    "UpdateLotteryRequest",
    "LotteryInfo",
    "WinnerTicket",
    "LotteryStatusSummary",
    "PanelToggleRequest",
    "MintBurnRequest",
    "ManualBankTransferRequest",
    "BankBalanceDTO",
    "CoinsStats",
    "PanelsStats",
    "ReferralsStats",
    "ShopStats",
    "SystemStats",
    "UserProgressSnapshot",
    "WithdrawRequestAdminDTO",
    "WithdrawFilters",
    "BonusPayoutFilters",
    "PrizeClaimFilters",
]

