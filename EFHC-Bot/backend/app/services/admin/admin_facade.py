"""Admin facade aggregator."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from .admin_ads_service import AdminAdsService
from .admin_bank_service import AdminBankService
from .admin_lotteries_service import AdminLotteriesService
from .admin_panels_service import AdminPanelsService
from .admin_referral_service import AdminReferralService
from .admin_settings import AdminSettings
from .admin_stats_service import AdminStatsService
from .admin_tasks_service import AdminTasksService
from .admin_users_service import AdminUsersService
from .admin_wallets_service import AdminWalletsService
from .admin_withdrawals_service import AdminWithdrawalsService


class AdminFacade:
    """Единая точка доступа к сервисам админки."""

    def __init__(self, session: AsyncSession):
        self.ads = AdminAdsService(session)
        self.bank = AdminBankService(session)
        self.lotteries = AdminLotteriesService(session)
        self.panels = AdminPanelsService(session)
        self.referrals = AdminReferralService(session)
        self.settings = AdminSettings()
        self.stats = AdminStatsService(session)
        self.tasks = AdminTasksService(session)
        self.users = AdminUsersService(session)
        self.wallets = AdminWalletsService(session)
        self.withdrawals = AdminWithdrawalsService(session)
