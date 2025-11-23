"""Сборка всех SQLAlchemy моделей EFHC.

Каждая модель вынесена в отдельный файл, но здесь собирается единая
точка импорта для CRUD/сервисов. Все таблицы используют DECIMAL(30, 8)
для денежных и энергетических значений в соответствии с каноном.
"""

from __future__ import annotations

from .achievements_models import Achievement
from .ads_models import AdsCampaign, AdsImpression
from .bank_models import BankState
from .lotteries_models import Lottery, LotteryTicket
from .order_models import AdjustmentOrder
from .panels_models import Panel
from .rating_models import RatingSnapshot
from .referral_models import Referral
from .shop_models import ShopItem, ShopOrder
from .tasks_models import Task, TaskSubmission
from .transactions_models import EFHCTransferLog, TonInboxLog
from .user_models import User

__all__ = [
    "Achievement",
    "AdsCampaign",
    "AdsImpression",
    "AdjustmentOrder",
    "BankState",
    "Lottery",
    "LotteryTicket",
    "Panel",
    "RatingSnapshot",
    "Referral",
    "ShopItem",
    "ShopOrder",
    "Task",
    "TaskSubmission",
    "EFHCTransferLog",
    "TonInboxLog",
    "User",
]
