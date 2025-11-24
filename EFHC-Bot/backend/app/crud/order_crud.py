"""CRUD layer for shop orders with read-through idempotency helpers."""

from __future__ import annotations

# ============================================================================
# EFHC Bot — crud/order_crud.py
# ---------------------------------------------------------------------------
# Назначение:
#   • Атомарные операции с заказами магазина (shop_orders) без изменения
#     балансов и бизнес-логики.
#   • Поддержка read-through идемпотентности по idempotency_key/tx_hash
#     для безопасного повторного создания/обновления заказов.
#
# Канон/инварианты:
#   • Модуль не двигает деньги и не меняет балансы пользователей/Банка.
#   • Только cursor-friendly выборки (ORDER BY created_at DESC, id DESC),
#     никаких OFFSET.
#   • Уникальность заказов обеспечивается на уровне схемы (UNIQUE tx_hash)
#     и сервисов (idempotency_key); здесь лишь удобные обёртки.
#   • P2P и EFHC→kWh отсутствуют; любые денежные движения выполняют сервисы
#     через банковский слой transactions_service.
#
# ИИ-защита/самовосстановление:
#   • create_or_get_by_idempotency() возвращает уже существующий заказ при
#     конфликте ключа вместо выброса — повторный вызов не создаёт дублей.
#   • Обновления статуса используют SELECT ... FOR UPDATE, чтобы избежать
#     гонок при финализации платежей.
#
# Запреты:
#   • Никакой бизнес-логики оплаты/доставки; только CRUD.
#   • Нет суточных ставок, нет вмешательства в балансы.
# ============================================================================

from datetime import datetime
from typing import Iterable, Optional

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.logging_core import get_logger
from backend.app.models.shop_models import ShopOrder

logger = get_logger(__name__)


class OrderCRUD:
    """CRUD-обёртка для shop_orders без денежных побочных эффектов."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_by_id(self, order_id: int) -> ShopOrder | None:
        """
        Получить заказ по первичному ключу.

        Назначение: безопасное чтение, без блокировок.
        Побочные эффекты: отсутствуют.
        """

        return await self.session.get(ShopOrder, int(order_id))

    async def create(self, order: ShopOrder) -> ShopOrder:
        """
        Сохранить новый заказ и вернуть его с первичным ключом.

        Назначение: аккуратное добавление без бизнес-логики. Балансы не
        меняются, commit выполняется на уровне вызывающего кода.
        Идемпотентность: не обеспечивает сама по себе; используйте
        create_or_get_by_idempotency, если нужен read-through.
        """

        self.session.add(order)
        await self.session.flush()
        return order

    async def get_by_idempotency_key(
        self, idempotency_key: str
    ) -> ShopOrder | None:
        """
        Найти заказ по idempotency_key (read-through).

        Используется для предотвращения дублей при повторном создании заказа
        фронтом/ботом.
        """

        stmt: Select[ShopOrder] = select(ShopOrder).where(
            ShopOrder.idempotency_key == idempotency_key
        )
        return await self.session.scalar(stmt)

    async def get_by_tx_hash(self, tx_hash: str) -> ShopOrder | None:
        """
        Найти заказ по уникальному tx_hash входящего TON-платежа.

        Используется вотчером/админкой для корреляции оплат.
        """

        stmt: Select[ShopOrder] = select(ShopOrder).where(
            ShopOrder.tx_hash == tx_hash
        )
        return await self.session.scalar(stmt)

    async def create_or_get_by_idempotency(
        self, order: ShopOrder
    ) -> ShopOrder:
        """
        Добавить заказ, если нет конфликта по idempotency_key/tx_hash.

        Идемпотентность: возвращает найденный заказ при существующем ключе,
        не создавая дублей. Денежные операции не выполняются.
        """

        if order.idempotency_key:
            existing = await self.get_by_idempotency_key(order.idempotency_key)
            if existing:
                return existing

        if order.tx_hash:
            existing_tx = await self.get_by_tx_hash(order.tx_hash)
            if existing_tx:
                return existing_tx

        self.session.add(order)
        await self.session.flush()
        return order

    async def attach_tx_hash_if_absent(
        self, order_id: int, tx_hash: str, memo: Optional[str] = None
    ) -> ShopOrder | None:
        """
        Присвоить tx_hash заказу, если он ещё не установлен.

        Назначение: используется вотчером/админкой, чтобы сопоставить входящий
        платёж с заказом. Не двигает деньги.
        Идемпотентность: повтор с тем же tx_hash просто вернёт актуальный
        заказ; значение не перезаписывается, если уже задано.
        """

        order = await self.session.get(
            ShopOrder, int(order_id), with_for_update=True
        )
        if order is None:
            return None

        if order.tx_hash is None:
            order.tx_hash = tx_hash
        if memo and not order.memo:
            order.memo = memo
        await self.session.flush()
        return order

    async def lock_and_update_status(
        self,
        order_id: int,
        *,
        status: str,
        tx_hash: Optional[str] = None,
        paid_at: Optional[datetime] = None,
        fulfilled_at: Optional[datetime] = None,
        memo: Optional[str] = None,
    ) -> ShopOrder | None:
        """
        Обновить статус заказа под блокировкой (FOR UPDATE).

        Назначение: финализация оплаты/выдачи без гонок.
        Побочные эффекты: изменяет поля заказа, но не балансы.
        Идемпотентность: повторное обновление того же заказа под тем же
        статусом не создаёт дублей и возвращает актуальное состояние.
        """

        order = await self.session.get(
            ShopOrder, int(order_id), with_for_update=True
        )
        if order is None:
            return None

        order.status = status
        if tx_hash and not order.tx_hash:
            order.tx_hash = tx_hash
        if memo and not order.memo:
            order.memo = memo
        if paid_at:
            order.paid_at = paid_at
        if fulfilled_at:
            order.fulfilled_at = fulfilled_at

        await self.session.flush()
        return order

    async def list_by_user_cursor(
        self,
        user_id: int,
        *,
        limit: int,
        cursor: tuple[datetime, int] | None = None,
    ) -> list[ShopOrder]:
        """
        Вернуть заказы пользователя с курсорной сортировкой.

        Курсор: (created_at, id) — строго меньше предыдущего курсора.
        Денежные операции не выполняются.
        """

        stmt: Select[ShopOrder] = (
            select(ShopOrder)
            .where(ShopOrder.user_id == int(user_id))
            .order_by(ShopOrder.created_at.desc(), ShopOrder.id.desc())
            .limit(limit)
        )

        if cursor:
            ts, oid = cursor
            stmt = stmt.where(
                (ShopOrder.created_at < ts)
                | ((ShopOrder.created_at == ts) & (ShopOrder.id < oid))
            )

        result: Iterable[ShopOrder] = await self.session.scalars(stmt)
        return list(result)

    async def list_cursor(
        self, *, limit: int, cursor: tuple[datetime, int] | None = None
    ) -> list[ShopOrder]:
        """
        Вернуть все заказы (например, для админ-списка) по курсору.

        Cursor: (created_at, id) — строго меньше предыдущего курсора.
        Денежные операции не выполняются.
        """

        stmt: Select[ShopOrder] = (
            select(ShopOrder)
            .order_by(ShopOrder.created_at.desc(), ShopOrder.id.desc())
            .limit(limit)
        )

        if cursor:
            ts, oid = cursor
            stmt = stmt.where(
                (ShopOrder.created_at < ts)
                | ((ShopOrder.created_at == ts) & (ShopOrder.id < oid))
            )

        result: Iterable[ShopOrder] = await self.session.scalars(stmt)
        return list(result)

    async def list_by_status_cursor(
        self,
        *,
        status: str,
        limit: int,
        cursor: tuple[datetime, int] | None = None,
    ) -> list[ShopOrder]:
        """
        Вернуть заказы по статусу с курсорной сортировкой.

        Используется админкой/вотчером для выборки PENDING/PAID заказов без
        OFFSET. Денег не двигает.
        """

        stmt: Select[ShopOrder] = (
            select(ShopOrder)
            .where(ShopOrder.status == status)
            .order_by(ShopOrder.created_at.desc(), ShopOrder.id.desc())
            .limit(limit)
        )

        if cursor:
            ts, oid = cursor
            stmt = stmt.where(
                (ShopOrder.created_at < ts)
                | ((ShopOrder.created_at == ts) & (ShopOrder.id < oid))
            )

        result: Iterable[ShopOrder] = await self.session.scalars(stmt)
        return list(result)


__all__ = ["OrderCRUD"]

# ============================================================================
# Пояснения «для чайника»:
#   • Этот модуль не двигает деньги и не знает про банковский сервис.
#   • create_or_get_by_idempotency() защищает от дублей при повторных
#     запросах фронта/бота.
#   • lock_and_update_status() использует FOR UPDATE, чтобы финализация
#     платежа не сталкивалась с гонками вотчера/админа.
#   • Пагинация только курсорная: created_at DESC, id DESC; OFFSET не нужен.
# ============================================================================
