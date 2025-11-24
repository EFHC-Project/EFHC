# -*- coding: utf-8 -*-
# backend/app/services/admin/admin_lotteries_service.py
# =============================================================================
# EFHC Bot — Лотереи (админ-сервис)
# -----------------------------------------------------------------------------
# Назначение:
#   • Канонический сервис управления лотереями в EFHC:
#       - создание/редактирование лотерей;
#       - включение/выключение/пауза;
#       - розыгрыш с идемпотентной фиксацией победителя;
#       - постановка в очередь призов:
#           • EFHC_BONUS → bonus_awards (только бонусный счёт),
#           • NFT_VIP    → prize_claims (NFT по кошельку).
#       - ручная коррекция билетов (БЕЗ P2P и без прямого трогания балансов).
#
# Жёсткие инварианты (ИИ-защита):
#   1) Никаких внутренних переводов между пользователями:
#        ПОЛЬЗОВАТЕЛЬ ↔ БАНК — единственно допустимое направление.
#   2) Призы EFHC в лотереях ВСЕГДА бонусные:
#        EFHC_BONUS → только на бонусный баланс пользователя.
#   3) Денежные операции (EFHC) — ТОЛЬКО через transactions_service,
#      но сам модуль ЛОТЕРЕЙ не меняет балансы напрямую:
#        • Он лишь создаёт bonus_awards / prize_claims.
#        • Фактическое начисление/списание делает WithdrawalsService/BankService.
#   4) Розыгрыш идемпотентен:
#        • Повторный вызов draw_lottery при наличии winner в lottery_winners
#          просто возвращает того же победителя и НЕ создаёт новые записи.
#   5) Лотерея может автоматически перезапускаться (auto_restart), если
#      флаг включён; новые лотереи создаются как копия шаблона.
#
# Таблицы (ожидаемая структура, без строгой привязки к миграциям):
#   • {SCHEMA_LOT}.lotteries(
#         id, title, prize_type, prize_value, ticket_price,
#         max_participants, max_tickets_per_user,
#         status,                -- DRAFT/ACTIVE/PAUSED/CLOSED/FINISHED
#         auto_restart BOOLEAN,  -- авто-перезапуск после FINISHED
#         auto_restart_delay_sec INT,  -- задержка перед авто-стартом (опц.)
#         created_at, finished_at
#     )
#
#   • {SCHEMA_LOT}.lottery_tickets(
#         id, lottery_id, user_id,
#         created_at,
#         idempotency_key TEXT NULL -- для user-side, если используется
#     )
#
#   • {SCHEMA_LOT}.lottery_winners(
#         id, lottery_id, ticket_id, user_id,
#         prize_type, prize_value, created_at
#     )
#
#   • {SCHEMA_ADMIN}.bonus_awards(
#         id, user_id, source, amount, status,
#         created_at, processed_at, meta_json,
#         processing_idempotency_key
#     )
#
#   • {SCHEMA_ADMIN}.prize_claims(
#         id, lottery_id, user_id, prize_type, prize_value,
#         wallet_address, status, created_at, processed_at,
#         reject_reason, tx_hash
#     )
#
# Важное замечание:
#   • Покупка билетов пользователями (списание EFHC) реализуется в
#     пользовательском сервисе (не здесь). Этот модуль даёт админам:
#       - управление метаданными лотерей,
#       - честный розыгрыш,
#       - ручную коррекцию билетов (без движения средств).
# =============================================================================

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, validator

from sqlalchemy import text
from sqlalchemy.engine import Result
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config_core import get_settings
from backend.app.core.logging_core import get_logger
from backend.app.core.utils_core import (
    quantize_decimal,
    format_decimal_str,
)

from .admin_rbac import AdminUser, AdminRole, RBAC
from .admin_logging import AdminLogger
from .admin_notifications import AdminNotifier

logger = get_logger(__name__)
S = get_settings()

SCHEMA_LOT: str = getattr(S, "DB_SCHEMA_LOTTERY", "efhc_lottery") or "efhc_lottery"
SCHEMA_CORE: str = getattr(S, "DB_SCHEMA_CORE", "efhc_core") or "efhc_core"
SCHEMA_ADMIN: str = getattr(S, "DB_SCHEMA_ADMIN", "efhc_admin") or "efhc_admin"

EFHC_DECIMALS: int = int(getattr(S, "EFHC_DECIMALS", 8) or 8)
_Q = Decimal(1).scaleb(-EFHC_DECIMALS)


def d8(x: Any) -> Decimal:
    """Округление вниз до EFHC_DECIMALS знаков."""
    return Decimal(str(x)).quantize(_Q, rounding=ROUND_DOWN)


# -----------------------------------------------------------------------------
# Перечисления и DTO
# -----------------------------------------------------------------------------

class LotteryStatus(str):
    """Статусы лотереи (управление жизненным циклом)."""
    DRAFT = "DRAFT"        # черновик (неактивна)
    ACTIVE = "ACTIVE"      # принимает билеты
    PAUSED = "PAUSED"      # временная пауза (билеты не принимаются)
    CLOSED = "CLOSED"      # приём билетов завершён, розыгрыш ещё не проведён
    FINISHED = "FINISHED"  # розыгрыш проведён, победитель зафиксирован


class LotteryPrizeType(str):
    """Типы призов лотереи: бонусные EFHC или NFT VIP."""
    EFHC_BONUS = "EFHC_BONUS"  # бонусные EFHC на бонусный счёт
    NFT_VIP = "NFT_VIP"        # NFT VIP (по привязанному кошельку)


class LotteryListFilters(BaseModel):
    """Фильтры списка лотерей для админки."""
    status: Optional[Literal["DRAFT", "ACTIVE", "PAUSED", "CLOSED", "FINISHED"]] = None
    title_substr: Optional[str] = None
    limit: int = Field(100, ge=1, le=500)
    offset: int = Field(0, ge=0)
    sort_desc: bool = True


class LotteryInfo(BaseModel):
    """Краткая карточка лотереи для списка."""
    id: int
    title: str
    prize_type: str
    prize_value: str
    ticket_price: str
    max_participants: int
    max_tickets_per_user: int
    status: str
    auto_restart: bool
    auto_restart_delay_sec: int
    created_at: str
    finished_at: Optional[str] = None


class CreateLotteryRequest(BaseModel):
    """
    Создание лотереи.

    prize_type:
      • EFHC_BONUS — призом будет бонусный EFHC на бонусный баланс.
      • NFT_VIP    — призом будет NFT VIP (запись в prize_claims).

    prize_value:
      • для EFHC_BONUS — Decimal (сколько EFHC начислить победителю),
      • для NFT_VIP    — произвольный маркер (по умолчанию 'NFT_VIP').
    """
    title: str = Field(..., min_length=1, max_length=120)
    prize_type: Literal["EFHC_BONUS", "NFT_VIP"]
    prize_value: Any = Field(..., description="Decimal для EFHC_BONUS или строковый маркер для NFT_VIP")
    ticket_price: Any = Field(..., description="Цена билета в EFHC (обычный EFHC)")
    max_participants: int = Field(500, ge=1, le=100000)
    max_tickets_per_user: int = Field(10, ge=1, le=1000)
    # Авто-перезапуск
    auto_restart: bool = Field(False, description="Авто-перезапуск новой лотереи после FINISHED")
    auto_restart_delay_sec: int = Field(0, ge=0, le=7 * 24 * 3600)

    @validator("ticket_price", pre=True)
    def _v_ticket_price(cls, v: Any) -> Decimal:
        return quantize_decimal(v, EFHC_DECIMALS, "DOWN")

    @validator("prize_value", pre=True)
    def _v_prize_value(cls, v: Any, values: Dict[str, Any]) -> Any:
        pt = values.get("prize_type")
        if pt == LotteryPrizeType.EFHC_BONUS:
            dec = quantize_decimal(v, EFHC_DECIMALS, "DOWN")
            if dec <= 0:
                raise ValueError("Значение prize_value для EFHC_BONUS должно быть > 0")
            return dec
        # NFT_VIP — любое нефустое строковое значение
        if isinstance(v, str) and v.strip():
            return v.strip()
        return "NFT_VIP"


class UpdateLotteryRequest(BaseModel):
    """Частичное обновление лотереи (только админ)."""
    title: Optional[str] = Field(default=None, min_length=1, max_length=120)
    ticket_price: Optional[Any] = Field(default=None)
    max_participants: Optional[int] = Field(default=None, ge=1, le=100000)
    max_tickets_per_user: Optional[int] = Field(default=None, ge=1, le=1000)
    status: Optional[Literal["DRAFT", "ACTIVE", "PAUSED", "CLOSED", "FINISHED"]] = None
    auto_restart: Optional[bool] = None
    auto_restart_delay_sec: Optional[int] = Field(default=None, ge=0, le=7 * 24 * 3600)

    @validator("ticket_price", pre=True)
    def _v_ticket_price(cls, v: Any) -> Optional[Decimal]:
        if v is None:
            return None
        return quantize_decimal(v, EFHC_DECIMALS, "DOWN")


class LotteryTicketCorrectionRequest(BaseModel):
    """
    Ручная корректировка билетов админом (БЕЗ движения средств):

      • delta_tickets > 0  → добавить N билетов пользователю;
      • delta_tickets < 0  → пометить N последних билетов пользователя как отменённые
                             (реализуется через таблицу lottery_tickets_corrections или
                             soft delete — реализуется БД/миграциями).
    ВАЖНО:
      • Эта операция не трогает EFHC-баланс. Если нужно компенсировать или
        доначислить EFHC, это делается отдельно через банк (admin_bank_service).
    """
    user_id: int
    lottery_id: int
    delta_tickets: int = Field(..., description="Положительное или отрицательное значение")
    reason: str = Field(..., min_length=1, max_length=500)


@dataclass
class WinnerTicket:
    """Описание победителя (для возврата в админ-панель)."""
    lottery_id: int
    ticket_id: int
    user_id: int
    prize_type: str
    prize_value: str
    user_wallet: Optional[str]


# -----------------------------------------------------------------------------
# Вспомогательные утилиты
# -----------------------------------------------------------------------------

def _now_utc() -> datetime:
    return datetime.now(tz=timezone.utc)


# -----------------------------------------------------------------------------
# Основной сервис лотерей
# -----------------------------------------------------------------------------

class AdminLotteryService:
    """
    Сервис администрирования лотерей EFHC.

    Для чайника:
      • Создание/редактирование лотерей — create_lottery()/update_lottery().
      • Включение/выключение/пауза:
          - activate_lottery()
          - pause_lottery()
          - close_lottery()
      • Розыгрыш:
          - draw_lottery() — выбирает случайный билет, записывает победителя,
            создаёт запись в bonus_awards или prize_claims.
      • Авто-перезапуск:
          - если у лотереи auto_restart=True, после FINISHED создаётся
            новая лотерея-клон.
      • Коррекция билетов:
          - adjust_tickets_admin() — только для коррекции кол-ва билетов,
            баланс EFHC не трогается.
    """

    # -------------------------------------------------------------------------
    # Список / чтение лотерей
    # -------------------------------------------------------------------------

    @staticmethod
    async def list_lotteries(db: AsyncSession, filters: LotteryListFilters) -> List[LotteryInfo]:
        """Список лотерей с фильтрами."""
        where: List[str] = ["1=1"]
        params: Dict[str, Any] = {"limit": filters.limit, "offset": filters.offset}

        if filters.status:
            where.append("status = :st")
            params["st"] = filters.status
        if filters.title_substr:
            where.append("title ILIKE :t")
            params["t"] = f"%{filters.title_substr}%"

        order = "DESC" if filters.sort_desc else "ASC"

        sql = text(
            f"""
            SELECT
                id,
                title,
                prize_type,
                prize_value,
                ticket_price,
                max_participants,
                max_tickets_per_user,
                status,
                COALESCE(auto_restart, FALSE) AS auto_restart,
                COALESCE(auto_restart_delay_sec, 0) AS auto_restart_delay_sec,
                created_at,
                finished_at
            FROM {SCHEMA_LOT}.lotteries
            WHERE {" AND ".join(where)}
            ORDER BY id {order}
            LIMIT :limit OFFSET :offset
            """
        )

        r: Result = await db.execute(sql, params)
        out: List[LotteryInfo] = []
        for row in r.fetchall():
            out.append(
                LotteryInfo(
                    id=int(row.id),
                    title=str(row.title),
                    prize_type=str(row.prize_type),
                    prize_value=str(row.prize_value),
                    ticket_price=str(row.ticket_price),
                    max_participants=int(row.max_participants),
                    max_tickets_per_user=int(row.max_tickets_per_user),
                    status=str(row.status),
                    auto_restart=bool(row.auto_restart),
                    auto_restart_delay_sec=int(row.auto_restart_delay_sec or 0),
                    created_at=row.created_at.isoformat() if hasattr(row.created_at, "isoformat") else str(row.created_at),
                    finished_at=row.finished_at.isoformat() if getattr(row, "finished_at", None) and hasattr(row.finished_at, "isoformat") else None,
                )
            )
        return out

    # -------------------------------------------------------------------------
    # Создание / обновление / управление статусом
    # -------------------------------------------------------------------------

    @staticmethod
    async def create_lottery(
        db: AsyncSession,
        req: CreateLotteryRequest,
        admin: AdminUser,
    ) -> int:
        """
        Создаёт новую лотерею.

        Канон:
          • prize_type:
              - EFHC_BONUS — приз только бонусными EFHC;
              - NFT_VIP    — приз только NFT (через prize_claims).
          • Лотерея сразу становится ACTIVE (по умолчанию), чтобы не
            плодить статус-классику — DRAFT можно поставить через update.
        """
        RBAC.require_role(admin, AdminRole.MODERATOR)

        ticket_price = quantize_decimal(req.ticket_price, EFHC_DECIMALS, "DOWN")
        if ticket_price <= 0:
            raise ValueError("Цена билета должна быть > 0")

        if req.prize_type == LotteryPrizeType.EFHC_BONUS:
            prize_val_str = format_decimal_str(req.prize_value, EFHC_DECIMALS)
        else:
            prize_val_str = str(req.prize_value)

        sql = text(
            f"""
            INSERT INTO {SCHEMA_LOT}.lotteries
                (title,
                 prize_type,
                 prize_value,
                 ticket_price,
                 max_participants,
                 max_tickets_per_user,
                 status,
                 auto_restart,
                 auto_restart_delay_sec,
                 created_at)
            VALUES
                (:title,
                 :ptype,
                 :pvalue,
                 :tprice,
                 :max_part,
                 :max_tpu,
                 'ACTIVE',
                 :auto_restart,
                 :auto_delay,
                 NOW() AT TIME ZONE 'UTC')
            RETURNING id
            """
        )
        r: Result = await db.execute(
            sql,
            {
                "title": req.title,
                "ptype": req.prize_type,
                "pvalue": prize_val_str,
                "tprice": format_decimal_str(ticket_price, EFHC_DECIMALS),
                "max_part": req.max_participants,
                "max_tpu": req.max_tickets_per_user,
                "auto_restart": bool(req.auto_restart),
                "auto_delay": int(req.auto_restart_delay_sec or 0),
            },
        )
        lid = int(r.scalar_one())

        await AdminLogger.write(
            db,
            admin_id=admin.id,
            action="LOTTERY_CREATE",
            entity="lottery",
            entity_id=lid,
            details=f"title={req.title}",
        )

        await AdminNotifier.notify_generic(
            db,
            event="LOTTERY_CREATED",
            message=f"Создана лотерея #{lid} «{req.title}» ({req.prize_type})",
        )

        return lid

    @staticmethod
    async def update_lottery(
        db: AsyncSession,
        lottery_id: int,
        req: UpdateLotteryRequest,
        admin: AdminUser,
    ) -> None:
        """
        Частичное обновление параметров лотереи.

        Особенности:
          • Статусы FINISHED/CLOSED считаются «почти финальными» — менять
            финансовые параметры после розыгрыша крайне нежелательно.
          • Код не запрещает обновление статуса, но админ должен понимать
            последствия (поэтому всё логируется).
        """
        RBAC.require_role(admin, AdminRole.MODERATOR)

        sets: List[str] = []
        params: Dict[str, Any] = {"id": lottery_id}

        if req.title is not None:
            sets.append("title = :title")
            params["title"] = req.title

        if req.ticket_price is not None:
            tprice = quantize_decimal(req.ticket_price, EFHC_DECIMALS, "DOWN")
            if tprice <= 0:
                raise ValueError("Цена билета должна быть > 0")
            sets.append("ticket_price = :tprice")
            params["tprice"] = format_decimal_str(tprice, EFHC_DECIMALS)

        if req.max_participants is not None:
            sets.append("max_participants = :max_participants")
            params["max_participants"] = req.max_participants

        if req.max_tickets_per_user is not None:
            sets.append("max_tickets_per_user = :max_tpu")
            params["max_tpu"] = req.max_tickets_per_user

        if req.status is not None:
            sets.append("status = :status")
            params["status"] = req.status

        if req.auto_restart is not None:
            sets.append("auto_restart = :auto_restart")
            params["auto_restart"] = bool(req.auto_restart)

        if req.auto_restart_delay_sec is not None:
            sets.append("auto_restart_delay_sec = :auto_delay")
            params["auto_delay"] = int(req.auto_restart_delay_sec)

        if not sets:
            return

        sql = text(
            f"""
            UPDATE {SCHEMA_LOT}.lotteries
            SET {", ".join(sets)}
            WHERE id = :id
            """
        )
        await db.execute(sql, params)

        await AdminLogger.write(
            db,
            admin_id=admin.id,
            action="LOTTERY_UPDATE",
            entity="lottery",
            entity_id=lottery_id,
            details=str(params),
        )

    @staticmethod
    async def activate_lottery(db: AsyncSession, lottery_id: int, admin: AdminUser) -> None:
        """Включает лотерею (status=ACTIVE)."""
        RBAC.require_role(admin, AdminRole.MODERATOR)
        await db.execute(
            text(
                f"""
                UPDATE {SCHEMA_LOT}.lotteries
                SET status='ACTIVE'
                WHERE id = :id
                """
            ),
            {"id": lottery_id},
        )
        await AdminLogger.write(
            db,
            admin_id=admin.id,
            action="LOTTERY_ACTIVATE",
            entity="lottery",
            entity_id=lottery_id,
            details="",
        )

    @staticmethod
    async def pause_lottery(db: AsyncSession, lottery_id: int, admin: AdminUser) -> None:
        """Ставит лотерею на паузу (status=PAUSED)."""
        RBAC.require_role(admin, AdminRole.MODERATOR)
        await db.execute(
            text(
                f"""
                UPDATE {SCHEMA_LOT}.lotteries
                SET status='PAUSED'
                WHERE id = :id
                """
            ),
            {"id": lottery_id},
        )
        await AdminLogger.write(
            db,
            admin_id=admin.id,
            action="LOTTERY_PAUSE",
            entity="lottery",
            entity_id=lottery_id,
            details="",
        )

    @staticmethod
    async def close_lottery(db: AsyncSession, lottery_id: int, admin: AdminUser) -> None:
        """
        Закрывает приём билетов (status=CLOSED), но ещё не проводит розыгрыш.

        Обычно вызывается автоматически после достижения лимита участников или
        по истечении времени.
        """
        RBAC.require_role(admin, AdminRole.MODERATOR)
        await db.execute(
            text(
                f"""
                UPDATE {SCHEMA_LOT}.lotteries
                SET status='CLOSED'
                WHERE id = :id
                """
            ),
            {"id": lottery_id},
        )
        await AdminLogger.write(
            db,
            admin_id=admin.id,
            action="LOTTERY_CLOSE",
            entity="lottery",
            entity_id=lottery_id,
            details="",
        )

    # -------------------------------------------------------------------------
    # РОЗЫГРЫШ ЛОТЕРЕИ
    # -------------------------------------------------------------------------

    @staticmethod
    async def _get_existing_winner(db: AsyncSession, lottery_id: int) -> Optional[WinnerTicket]:
        """Проверяет, есть ли уже победитель (идемпотентность draw_lottery)."""
        sql = text(
            f"""
            SELECT
                w.ticket_id,
                w.user_id,
                w.prize_type,
                w.prize_value
            FROM {SCHEMA_LOT}.lottery_winners w
            WHERE w.lottery_id = :lid
            LIMIT 1
            """
        )
        r: Result = await db.execute(sql, {"lid": lottery_id})
        row = r.fetchone()
        if not row:
            return None

        # Кошелёк ищем по user_id (primary)
        r2: Result = await db.execute(
            text(
                f"""
                SELECT address
                FROM {SCHEMA_CORE}.crypto_wallets
                WHERE user_id = :uid AND is_primary = TRUE
                ORDER BY id DESC
                LIMIT 1
                """
            ),
            {"uid": row.user_id},
        )
        wrow = r2.fetchone()
        wallet = wrow.address if wrow else None

        return WinnerTicket(
            lottery_id=lottery_id,
            ticket_id=int(row.ticket_id),
            user_id=int(row.user_id),
            prize_type=str(row.prize_type),
            prize_value=str(row.prize_value),
            user_wallet=wallet,
        )

    @staticmethod
    async def draw_lottery(
        db: AsyncSession,
        *,
        lottery_id: int,
        admin: AdminUser,
    ) -> Optional[WinnerTicket]:
        """
        Проводит честный розыгрыш лотереи:

          1) Проверяет, не был ли уже выбран победитель (lottery_winners).
             Если да — возвращает его (идемпотентный повтор).
          2) Выбирает случайный билет ORDER BY random() LIMIT 1.
          3) Записывает победителя в lottery_winners.
          4) Ставит лотерею в статус FINISHED + finished_at.
          5) Создаёт запись:
               • EFHC_BONUS → bonus_awards (PENDING),
               • NFT_VIP    → prize_claims  (PENDING).
          6) При включённом auto_restart создаёт новую лотерею-клон.

        Возвращает WinnerTicket или None, если билетов не было.
        """
        RBAC.require_role(admin, AdminRole.MODERATOR)

        # 1) Проверяем существующего победителя
        existing = await AdminLotteryService._get_existing_winner(db, lottery_id)
        if existing:
            logger.info("draw_lottery(%s): winner already exists → idempotent return", lottery_id)
            return existing

        # 2) Читаем метаданные лотереи
        rmeta: Result = await db.execute(
            text(
                f"""
                SELECT
                    id,
                    title,
                    prize_type,
                    prize_value,
                    ticket_price,
                    max_participants,
                    max_tickets_per_user,
                    status,
                    COALESCE(auto_restart, FALSE) AS auto_restart,
                    COALESCE(auto_restart_delay_sec, 0) AS auto_restart_delay_sec
                FROM {SCHEMA_LOT}.lotteries
                WHERE id = :lid
                LIMIT 1
                """
            ),
            {"lid": lottery_id},
        )
        meta = rmeta.fetchone()
        if not meta:
            raise ValueError("Лотерея не найдена")

        if meta.status not in (LotteryStatus.ACTIVE, LotteryStatus.CLOSED):
            # Не даём случайно переиграть FINISHED/DRAFT/PAUSED
            raise ValueError(f"Лотерея в статусе {meta.status}, розыгрыш невозможен")

        # 3) Ищем случайный билет
        rt: Result = await db.execute(
            text(
                f"""
                SELECT lt.id AS ticket_id, lt.user_id
                FROM {SCHEMA_LOT}.lottery_tickets lt
                WHERE lt.lottery_id = :lid
                ORDER BY random()
                LIMIT 1
                """
            ),
            {"lid": lottery_id},
        )
        ticket = rt.fetchone()
        if not ticket:
            logger.info("draw_lottery(%s): нет билетов, победитель не выбран", lottery_id)
            # Логически можно перевести лотерею в CLOSED/FINISHED, но оставим
            # на усмотрение админов.
            return None

        ticket_id = int(ticket.ticket_id)
        user_id = int(ticket.user_id)

        # 4) Записываем победителя (ON CONFLICT на lottery_id — защита от гонок)
        await db.execute(
            text(
                f"""
                INSERT INTO {SCHEMA_LOT}.lottery_winners
                    (lottery_id, ticket_id, user_id, prize_type, prize_value, created_at)
                VALUES
                    (:lid, :tid, :uid, :ptype, :pvalue, NOW() AT TIME ZONE 'UTC')
                ON CONFLICT (lottery_id) DO NOTHING
                """
            ),
            {
                "lid": lottery_id,
                "tid": ticket_id,
                "uid": user_id,
                "ptype": meta.prize_type,
                "pvalue": str(meta.prize_value),
            },
        )

        # На случай гонки: если кто-то опередил, просто вернём существующего победителя
        existing_after = await AdminLotteryService._get_existing_winner(db, lottery_id)
        if existing_after and (existing_after.ticket_id != ticket_id or existing_after.user_id != user_id):
            logger.warning(
                "draw_lottery(%s): race detected, another winner already stored (ticket_id=%s,user_id=%s) → returning stored",
                lottery_id,
                existing_after.ticket_id,
                existing_after.user_id,
            )
            return existing_after

        # 5) Переводим лотерею в FINISHED
        await db.execute(
            text(
                f"""
                UPDATE {SCHEMA_LOT}.lotteries
                SET status='FINISHED',
                    finished_at = NOW() AT TIME ZONE 'UTC'
                WHERE id = :lid
                """
            ),
            {"lid": lottery_id},
        )

        # 6) Создаём запись о призе
        user_wallet: Optional[str] = None

        if meta.prize_type == LotteryPrizeType.EFHC_BONUS:
            # Приз – бонусные EFHC → bonus_awards (без немедленного начисления)
            prize_amount = d8(meta.prize_value)
            await db.execute(
                text(
                    f"""
                    INSERT INTO {SCHEMA_ADMIN}.bonus_awards
                        (user_id, source, amount, status, created_at, meta_json)
                    VALUES
                        (:uid, 'LOTTERY', :amt, 'PENDING', NOW() AT TIME ZONE 'UTC',
                         jsonb_build_object('lottery_id', :lid))
                    """
                ),
                {
                    "uid": user_id,
                    "amt": format_decimal_str(prize_amount, EFHC_DECIMALS),
                    "lid": lottery_id,
                },
            )
        elif meta.prize_type == LotteryPrizeType.NFT_VIP:
            # Приз — NFT VIP → prize_claims
            r2: Result = await db.execute(
                text(
                    f"""
                    SELECT address
                    FROM {SCHEMA_CORE}.crypto_wallets
                    WHERE user_id = :uid AND is_primary = TRUE
                    ORDER BY id DESC
                    LIMIT 1
                    """
                ),
                {"uid": user_id},
            )
            wrow = r2.fetchone()
            user_wallet = wrow.address if wrow else None

            await db.execute(
                text(
                    f"""
                    INSERT INTO {SCHEMA_ADMIN}.prize_claims
                        (lottery_id, user_id, prize_type, prize_value, wallet_address, status, created_at)
                    VALUES
                        (:lid, :uid, 'NFT_VIP', 'NFT_VIP', :wallet, 'PENDING', NOW() AT TIME ZONE 'UTC')
                    """
                ),
                {"lid": lottery_id, "uid": user_id, "wallet": user_wallet},
            )

        # Логи + уведомления
        await AdminLogger.write(
            db,
            admin_id=admin.id,
            action="LOTTERY_DRAW",
            entity="lottery",
            entity_id=lottery_id,
            details=f"winner_user_id={user_id}; ticket_id={ticket_id}",
        )

        await AdminNotifier.notify_lottery_winner(
            db,
            lottery_id=lottery_id,
            user_id=user_id,
            prize=str(meta.prize_value),
            title=meta.title,
        )

        # 7) Авто-перезапуск, если включён
        if bool(meta.auto_restart):
            await AdminLotteryService._auto_restart_clone(db, meta)

        return WinnerTicket(
            lottery_id=lottery_id,
            ticket_id=ticket_id,
            user_id=user_id,
            prize_type=str(meta.prize_type),
            prize_value=str(meta.prize_value),
            user_wallet=user_wallet,
        )

    @staticmethod
    async def _auto_restart_clone(db: AsyncSession, meta: Any) -> None:
        """
        Внутренняя функция: создаёт новую лотерею-клон на основе meta
        при включённом auto_restart.

        Важно:
          • Статус новой лотереи — ACTIVE.
          • Параметры prize_type/prize_value/ticket_price/per-user лимиты
            просто копируются.
          • Задержка запуска (auto_restart_delay_sec) реализуется через
            created_at (можно добавить start_at через миграцию, если нужно).
        """
        delay = int(getattr(meta, "auto_restart_delay_sec", 0) or 0)
        # created_at всё равно NOW(), delay реализуется на уровне планировщика/роутов
        sql = text(
            f"""
            INSERT INTO {SCHEMA_LOT}.lotteries
                (title,
                 prize_type,
                 prize_value,
                 ticket_price,
                 max_participants,
                 max_tickets_per_user,
                 status,
                 auto_restart,
                 auto_restart_delay_sec,
                 created_at)
            VALUES
                (:title,
                 :ptype,
                 :pvalue,
                 :tprice,
                 :max_part,
                 :max_tpu,
                 'ACTIVE',
                 :auto_restart,
                 :auto_delay,
                 NOW() AT TIME ZONE 'UTC')
            """
        )
        await db.execute(
            sql,
            {
                "title": meta.title,
                "ptype": meta.prize_type,
                "pvalue": str(meta.prize_value),
                "tprice": str(meta.ticket_price),
                "max_part": meta.max_participants,
                "max_tpu": meta.max_tickets_per_user,
                "auto_restart": True,
                "auto_delay": delay,
            },
        )
        logger.info("auto_restart: создан клон лотереи из шаблона id=%s", meta.id)

    # -------------------------------------------------------------------------
    # РУЧНАЯ КОРРЕКЦИЯ БИЛЕТОВ (БЕЗ ДВИЖЕНИЯ EFHC)
    # -------------------------------------------------------------------------

    @staticmethod
    async def adjust_tickets_admin(
        db: AsyncSession,
        req: LotteryTicketCorrectionRequest,
        admin: AdminUser,
    ) -> Dict[str, Any]:
        """
        Ручная корректировка билетов админом.

        Инварианты:
          • НИКАКИХ денежных операций здесь нет — только операции над билетами.
          • Для компенсации/доначисления EFHC использовать банк (admin_bank_service).
          • Действия логируются в admin_logs.

        Логика:
          • delta_tickets > 0:
               - добавляем N новых билетов пользователю в указанной лотерее;
          • delta_tickets < 0:
               - помечаем N последних билетов как «исправленные/отменённые».
                 Реализация через:
                   - отдельную таблицу lottery_tickets_corrections,
                   - или поле is_cancelled в lottery_tickets (зависит от миграций).
                 В этом коде предполагается флаг is_cancelled BOOLEAN.
        """
        RBAC.require_role(admin, AdminRole.MODERATOR)

        if req.delta_tickets == 0:
            raise ValueError("delta_tickets не может быть 0")

        # Проверяем, что лотерея существует
        rlot: Result = await db.execute(
            text(
                f"""
                SELECT id, status
                FROM {SCHEMA_LOT}.lotteries
                WHERE id = :lid
                LIMIT 1
                """
            ),
            {"lid": req.lottery_id},
        )
        lrow = rlot.fetchone()
        if not lrow:
            raise ValueError("Лотерея не найдена")

        # Для finished/closed — админ должен понимать последствия, но мы не запрещаем.
        affected = 0

        if req.delta_tickets > 0:
            # Добавляем N билетов
            sql = text(
                f"""
                INSERT INTO {SCHEMA_LOT}.lottery_tickets
                    (lottery_id, user_id, created_at)
                VALUES
                    (:lid, :uid, NOW() AT TIME ZONE 'UTC')
                """
            )
            for _ in range(req.delta_tickets):
                await db.execute(sql, {"lid": req.lottery_id, "uid": req.user_id})
                affected += 1
        else:
            # delta_tickets < 0 → отмена N последних билетов пользователя
            to_cancel = abs(req.delta_tickets)
            sql_sel = text(
                f"""
                SELECT id
                FROM {SCHEMA_LOT}.lottery_tickets
                WHERE lottery_id = :lid
                  AND user_id   = :uid
                  AND COALESCE(is_cancelled, FALSE) = FALSE
                ORDER BY id DESC
                LIMIT :lim
                """
            )
            rtk: Result = await db.execute(
                sql_sel,
                {"lid": req.lottery_id, "uid": req.user_id, "lim": to_cancel},
            )
            ticket_ids = [int(x.id) for x in rtk.fetchall()]
            if ticket_ids:
                sql_upd = text(
                    f"""
                    UPDATE {SCHEMA_LOT}.lottery_tickets
                    SET is_cancelled = TRUE
                    WHERE id = ANY(:ids)
                    """
                )
                await db.execute(sql_upd, {"ids": ticket_ids})
                affected = len(ticket_ids)

        await AdminLogger.write(
            db,
            admin_id=admin.id,
            action="LOTTERY_TICKETS_ADJUST",
            entity="lottery_tickets",
            entity_id=req.lottery_id,
            details=f"user_id={req.user_id}; delta={req.delta_tickets}; reason={req.reason}",
        )

        return {"ok": True, "affected": affected}


__all__ = [
    "LotteryStatus",
    "LotteryPrizeType",
    "LotteryListFilters",
    "LotteryInfo",
    "CreateLotteryRequest",
    "UpdateLotteryRequest",
    "LotteryTicketCorrectionRequest",
    "WinnerTicket",
    "AdminLotteryService",
]

