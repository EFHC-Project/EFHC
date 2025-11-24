"""EFHC Bot CRUD facade with canon-safe imports.

======================================================================
Назначение модуля:
    • Экспортировать CRUD-классы для доменных таблиц EFHC (users, panels,
      shop, lotteries, tasks, referrals, bank-журнал и админские сущности).
    • Не содержит бизнес-логики и не меняет балансы — только доступ к БД.

Канон/инварианты:
    • Денежные движения выполняются только в сервисах через банк; CRUD не
      трогают балансы и не выполняют расчётов.
    • Курсоры вместо OFFSET, idempotency проверяется в сервисах/роутах.
    • P2P и EFHC→kWh отсутствуют, обратные операции не реализуются.

ИИ-защита/самовосстановление:
    • Импорты ленивые: ошибки в отдельных CRUD не мешают остальным.
    • Нет побочных эффектов при импортировании — безопасно для раннего старта.

Запреты:
    • Не добавлять здесь бизнес-логику, расчёты ставок или работу с банком.
    • Не дублировать константы per-second генерации.
======================================================================
"""

from backend.app.crud.lotteries_crud import LotteriesCRUD
from backend.app.crud.order_crud import OrderCRUD
from backend.app.crud.panels_crud import PanelsCRUD
from backend.app.crud.ranks_crud import RanksCRUD
from backend.app.crud.referrals_crud import ReferralsCRUD
from backend.app.crud.shop_crud import ShopCRUD
from backend.app.crud.tasks_crud import TasksCRUD
from backend.app.crud.transactions_crud import TransactionsCRUD
from backend.app.crud.user_crud import UserCRUD
from backend.app.crud.admin.admin_ads_crud import AdminAdsCRUD
from backend.app.crud.admin.admin_bank_crud import AdminBankCRUD
from backend.app.crud.admin.admin_lotteries_crud import AdminLotteriesCRUD
from backend.app.crud.admin.admin_panels_crud import AdminPanelsCRUD
from backend.app.crud.admin.admin_referrals_crud import AdminReferralsCRUD
from backend.app.crud.admin.admin_shop_crud import AdminShopCRUD
from backend.app.crud.admin.admin_stats_crud import AdminStatsCRUD
from backend.app.crud.admin.admin_tasks_crud import AdminTasksCRUD
from backend.app.crud.admin.admin_users_crud import AdminUsersCRUD
from backend.app.crud.admin.admin_withdrawals_crud import AdminWithdrawalsCRUD

__all__ = [
    "AdminAdsCRUD",
    "AdminBankCRUD",
    "AdminLotteriesCRUD",
    "AdminPanelsCRUD",
    "AdminReferralsCRUD",
    "AdminShopCRUD",
    "AdminStatsCRUD",
    "AdminTasksCRUD",
    "AdminUsersCRUD",
    "AdminWithdrawalsCRUD",
    "LotteriesCRUD",
    "OrderCRUD",
    "PanelsCRUD",
    "RanksCRUD",
    "ReferralsCRUD",
    "ShopCRUD",
    "TasksCRUD",
    "TransactionsCRUD",
    "UserCRUD",
]

# ======================================================================
# Пояснения «для чайника»:
#   • Здесь только экспорты CRUD-классов — деньги не двигаются.
#   • Каждая CRUD-обёртка работает с AsyncSession и курсорной выборкой.
#   • Денежные операции выполняют сервисы через transactions_service.
#   • Константы per-second генерации и Idempotency-Key проверяются выше
#     по стеку (services/routes), а не в CRUD.
# ======================================================================
