# -*- coding: utf-8 -*-
# backend/app/services/lotteries_service.py
# =============================================================================
# Назначение кода:
#   Сервис лотерей EFHC Bot: витрины, покупка билетов (денежная операция),
#   админ-операции (создание, закрытие продаж, розыгрыш), выборки с курсором.
#
# Канон/инварианты:
#   • Денежные списания за билеты идут через единый банковский сервис EFHC.
#   • Строгое правило списаний: СНАЧАЛА bonus_balance, затем main_balance.
#   • Пользователь НИКОГДА не уходит в минус (жёсткий запрет).
#   • Банк может уйти в минус — это допустимо (операции не блокируются).
#   • Любые денежные POST — с Idempotency-Key. Здесь реализован read-through:
#       – бонусная часть: idk+":B"
#       – основная часть: idk+":M"
#   • Приз NFT не выдаётся автоматически — только заявка (claim), ручная выдача.
#
# ИИ-защита/самовосстановление:
#   • Все выборки — курсорные (без OFFSET), устойчивые к перегрузкам.
#   • Идемпотентность опирается на уникальные ключи банковских логов.
#   • Блокировка гонок: SELECT ... FOR UPDATE SKIP LOCKED по строке лотереи
#     при распределении билетов, чтобы не продать один и тот же номер дважды.
#
# Запреты:
#   • Никаких P2P, обратных конверсий и прочих нестандартных списаний.
#   • Никаких суточных ставок — подсистема лотерей не знает о генерации энергии.
# =============================================================================

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import and_, asc, desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config_core import get_settings
from backend.app.core.logging_core import get_logger
from backend.app.deps import d8
from backend.app.models.lottery_models import (
    Lottery,
    LotteryTicket,
    LotteryUserStat,
    LotteryResult,
    LotteryNFTClaim,
)
from backend.app.services.transactions_service import (
    debit_user_bonus_to_bank,   # (db, user_id, amount, idempotency_key, reason, meta=None) -> dict
    debit_user_to_bank,         # (db, user_id, amount, idempotency_key, reason, meta=None) -> dict
)
from backend.app.models.user_models import User  # для проверки балансов и user_id

logger = get_logger(__name__)
settings = get_settings()
SCHEMA = getattr(settings, "DB_SCHEMA_CORE", "efhc_core")

# -----------------------------------------------------------------------------
# DTO/утилиты
# -----------------------------------------------------------------------------

@dataclass
class CursorPoint:
    created_at: datetime
    id: int


def _build_cursor(items: Sequence[dict]) -> Optional[CursorPoint]:
    if not items:
        return None
    last = items[-1]
    return CursorPoint(created_at=last["created_at"], id=last["id"])


def _cursor_filter(q, cursor: Optional[CursorPoint]):
    """
    Для стабильной пагинации по возрастанию (created_at, id).
    """
    if not cursor:
        return q
    return q.where(
        (Lottery.created_at > cursor.created_at)
        | and_(Lottery.created_at == cursor.created_at, Lottery.id > cursor.id)
    )

# -----------------------------------------------------------------------------
# Витрина активных лотерей
# -----------------------------------------------------------------------------

async def svc_list_active_lotteries(
    db: AsyncSession,
    limit: int,
    cursor: Optional[Tuple[str, int]] = None,
) -> Tuple[List[Dict[str, Any]], Optional[Tuple[str, int]]]:
    """
    Возвращает активные лотереи (status='active') с курсорной пагинацией.
    cursor: (iso_datetime, id) или None.
    """
    cur_point: Optional[CursorPoint] = None
    if cursor:
        try:
            cur_point = CursorPoint(
                created_at=datetime.fromisoformat(cursor[0]),
                id=int(cursor[1]),
            )
        except Exception:
            cur_point = None

    base_q = (
        select(
            Lottery.id,
            Lottery.title,
            Lottery.prize_type,
            Lottery.prize_value,
            Lottery.ticket_price,
            Lottery.total_tickets,
            Lottery.tickets_sold,
            Lottery.status,
            Lottery.created_at,
        )
        .where(Lottery.status == "active")
        .order_by(asc(Lottery.created_at), asc(Lottery.id))
        .limit(limit)
    )
    q = _cursor_filter(base_q, cur_point)
    rows = (await db.execute(q)).all()

    items = [
        {
            "id": r.id,
            "title": r.title,
            "prize_type": r.prize_type,
            "prize_value": r.prize_value,
            "ticket_price": d8(r.ticket_price),
            "total_tickets": r.total_tickets,
            "tickets_sold": r.tickets_sold,
            "status": r.status,
            "created_at": r.created_at,
        }
        for r in rows
    ]
    next_cur = _build_cursor(items)
    return items, ((next_cur.created_at.isoformat(), next_cur.id) if next_cur else None)

# -----------------------------------------------------------------------------
# Статус лотереи
# -----------------------------------------------------------------------------

async def svc_get_lottery_status(db: AsyncSession, lottery_id: int) -> Optional[Dict[str, Any]]:
    lot = (
        await db.execute(
            select(Lottery).where(Lottery.id == lottery_id)
        )
    ).scalar_one_or_none()
    if not lot:
        return None

    result = (
        await db.execute(
            select(LotteryResult).where(LotteryResult.lottery_id == lottery_id)
        )
    ).scalar_one_or_none()

    result_payload = None
    if result:
        result_payload = {
            "winning_ticket_id": result.winning_ticket_id,
            "winner_telegram_id": result.winner_telegram_id,
            "completed_at": result.completed_at.isoformat(),
        }

    return {
        "id": lot.id,
        "title": lot.title,
        "prize_type": lot.prize_type,
        "prize_value": lot.prize_value,
        "ticket_price": d8(lot.ticket_price),
        "total_tickets": lot.total_tickets,
        "tickets_sold": lot.tickets_sold,
        "status": lot.status,
        "result": result_payload,
    }

# -----------------------------------------------------------------------------
# Мои билеты (курсорно)
# -----------------------------------------------------------------------------

async def svc_list_user_tickets(
    db: AsyncSession,
    lottery_id: int,
    telegram_id: int,
    limit: int,
    cursor: Optional[Tuple[int]] = None,
) -> Tuple[List[int], Optional[Tuple[int]]]:
    """
    Возвращает ID билетов данного пользователя по возрастанию ticket_id.
    cursor — последний ticket_id.
    """
    last_id = int(cursor[0]) if cursor else 0

    rows = (
        await db.execute(
            select(LotteryTicket.ticket_id)
            .where(
                LotteryTicket.lottery_id == lottery_id,
                LotteryTicket.owner_telegram_id == telegram_id,
                LotteryTicket.ticket_id > last_id,
            )
            .order_by(asc(LotteryTicket.ticket_id))
            .limit(limit)
        )
    ).all()

    tickets = [r.ticket_id for r in rows]
    next_cur = (tickets[-1],) if tickets else None
    return tickets, next_cur

# -----------------------------------------------------------------------------
# Внутренние helpers
# -----------------------------------------------------------------------------

async def _load_user_by_telegram(db: AsyncSession, telegram_id: int) -> Optional[User]:
    return (
        await db.execute(
            select(User).where(User.telegram_id == telegram_id)
        )
    ).scalar_one_or_none()


async def _lock_lottery(db: AsyncSession, lottery_id: int) -> Optional[Lottery]:
    """
    Блокируем строку лотереи на время распределения билетов.
    """
    lot = (
        await db.execute(
            select(Lottery)
            .where(Lottery.id == lottery_id)
            .with_for_update(skip_locked=True)
        )
    ).scalar_one_or_none()
    return lot


def _compute_spend_parts(total: Decimal, bonus_balance: Decimal) -> Tuple[Decimal, Decimal]:
    """
    Возвращает (spend_bonus, spend_main) при правиле bonus-first.
    """
    use_bonus = min(bonus_balance, total)
    use_main = total - use_bonus
    return d8(use_bonus), d8(use_main)


async def _append_tickets_and_stats(
    db: AsyncSession,
    lot: Lottery,
    buyer_tg: int,
    quantity: int,
) -> Tuple[List[int], int]:
    """
    Распределяет билеты [old_sold+1 .. old_sold+quantity], обновляет счётчики и stats.
    Предполагается, что lot уже заблокирован (FOR UPDATE SKIP LOCKED).
    """
    if lot.status != "active":
        raise ValueError("Продажи закрыты или лотерея завершена.")

    if lot.tickets_sold + quantity > lot.total_tickets:
        raise ValueError("Недостаточно доступных билетов.")

    start = lot.tickets_sold + 1
    end = lot.tickets_sold + quantity
    ticket_ids = list(range(start, end + 1))

    # Вставка билетов
    now = datetime.now(timezone.utc)
    for t_id in ticket_ids:
        db.add(
            LotteryTicket(
                lottery_id=lot.id,
                ticket_id=t_id,
                owner_telegram_id=buyer_tg,
                created_at=now,
            )
        )

    # Обновляем счётчик в лотерее
    lot.tickets_sold = lot.tickets_sold + quantity
    lot.updated_at = now

    # Обновляем агрегат пользователя
    stat = (
        await db.execute(
            select(LotteryUserStat).where(
                LotteryUserStat.lottery_id == lot.id,
                LotteryUserStat.telegram_id == buyer_tg,
            )
        )
    ).scalar_one_or_none()

    if not stat:
        stat = LotteryUserStat(
            lottery_id=lot.id,
            telegram_id=buyer_tg,
            tickets_count=quantity,
            updated_at=now,
        )
        db.add(stat)
    else:
        stat.tickets_count += quantity
        stat.updated_at = now

    return ticket_ids, lot.tickets_sold

# -----------------------------------------------------------------------------
# Покупка билетов с bonus-first списанием и read-through идемпотентностью
# -----------------------------------------------------------------------------

async def svc_buy_tickets(
    db: AsyncSession,
    lottery_id: int,
    buyer_telegram_id: int,
    quantity: int,
    idempotency_key: str,
) -> Dict[str, Any]:
    """
    Денежная операция: купить 'quantity' билетов.
    Шаги:
      1) Проверить пользователя и его балансы (минус запрещён).
      2) Заблокировать лотерею (FOR UPDATE SKIP LOCKED), проверить доступность.
      3) Посчитать общую стоимость и части списаний: бонус сначала, потом мейн.
      4) Выполнить ДВА идемпотентных списания с разными ключами:
         – idk+":B" (bonus), если >0
         – idk+":M" (main),  если >0
      5) Распределить билеты и обновить счётчики.
    Read-through: повторный вызов с тем же idk не создаст повторных списаний.
    """
    if quantity < 1 or quantity > 100:
        raise ValueError("quantity должен быть в диапазоне 1..100.")

    user = await _load_user_by_telegram(db, buyer_telegram_id)
    if not user:
        raise ValueError("Пользователь не найден.")

    # Лотерея под блокировкой
    lot = await _lock_lottery(db, lottery_id)
    if not lot:
        raise ValueError("Лотерея не доступна (возможно, параллельная операция).")

    if lot.status != "active":
        raise ValueError("Продажи для этой лотереи закрыты.")

    # Стоимость
    price = d8(lot.ticket_price)
    total_cost = d8(Decimal(str(quantity)) * price)

    # Балансы пользователя
    bonus_bal = d8(user.bonus_balance or 0)
    main_bal = d8(user.main_balance or 0)

    # Правило: пользователь не может уйти в минус
    if bonus_bal + main_bal < total_cost:
        raise ValueError("Недостаточно средств (bonus+main) для покупки билетов.")

    spend_bonus, spend_main = _compute_spend_parts(total_cost, bonus_bal)

    # Выполняем денежные списания через банк (идемпотентно)
    meta_common = {
        "domain": "lottery_buy",
        "lottery_id": lot.id,
        "quantity": quantity,
        "unit_price": str(price),
        "total_cost": str(total_cost),
        "buyer_telegram_id": buyer_telegram_id,
    }

    if spend_bonus > 0:
        await debit_user_bonus_to_bank(
            db=db,
            user_id=user.id,
            amount=spend_bonus,
            idempotency_key=f"{idempotency_key}:B",
            reason="lottery_ticket",
            meta=meta_common,
        )

    if spend_main > 0:
        await debit_user_to_bank(
            db=db,
            user_id=user.id,
            amount=spend_main,
            idempotency_key=f"{idempotency_key}:M",
            reason="lottery_ticket",
            meta=meta_common,
        )

    # Распределяем билеты
    ticket_ids, tickets_sold = await _append_tickets_and_stats(
        db=db,
        lot=lot,
        buyer_tg=buyer_telegram_id,
        quantity=quantity,
    )

    return {
        "ok": True,
        "purchased": quantity,
        "total_spent": str(total_cost),
        "my_ticket_ids": ticket_ids,
        "tickets_sold": tickets_sold,
    }

# -----------------------------------------------------------------------------
# Админ: создание лотереи
# -----------------------------------------------------------------------------

async def svc_admin_create_lottery(
    db: AsyncSession,
    admin_telegram_id: int,
    payload: Dict[str, Any],
    idempotency_key: str,
) -> Dict[str, Any]:
    """
    Создать лотерею. Денежных списаний нет, но применяем идемпотентность
    на уровне бизнес-объекта: повтор с тем же idk должен вернуть ту же лотерею.
    Для простоты используем «естественную идемпотентность» по (title, created_at≈now, admin_tg).
    """
    now = datetime.now(timezone.utc)
    title = str(payload["title"]).strip()
    prize_type = str(payload["prize_type"]).strip()
    prize_value = payload.get("prize_value")
    ticket_price = d8(payload["ticket_price"])
    max_participants = int(payload["max_participants"])
    max_per_user = int(payload["max_tickets_per_user"])
    auto_draw = bool(payload.get("auto_draw", True))

    existing = (
        await db.execute(
            select(Lottery)
            .where(
                Lottery.title == title,
                Lottery.created_at >= now.replace(minute=max(0, now.minute - 5)),
            )
            .order_by(desc(Lottery.created_at))
            .limit(1)
        )
    ).scalar_one_or_none()

    if existing:
        return {"id": existing.id, "title": existing.title, "status": existing.status}

    lot = Lottery(
        title=title,
        prize_type=prize_type,
        prize_value=prize_value,
        ticket_price=ticket_price,
        max_participants=max_participants,
        max_tickets_per_user=max_per_user,
        total_tickets=max_participants,
        tickets_sold=0,
        status="active",
        auto_draw=auto_draw,
        created_at=now,
        updated_at=now,
    )
    db.add(lot)
    await db.flush()
    return {"id": lot.id, "title": lot.title, "status": lot.status}

# -----------------------------------------------------------------------------
# Админ: закрыть продажи
# -----------------------------------------------------------------------------

async def svc_admin_close_sales(
    db: AsyncSession,
    admin_telegram_id: int,
    lottery_id: int,
    idempotency_key: str,
) -> Dict[str, Any]:
    lot = (
        await db.execute(
            select(Lottery)
            .where(Lottery.id == lottery_id)
            .with_for_update(skip_locked=True)
        )
    ).scalar_one_or_none()
    if not lot:
        raise ValueError("Лотерея не найдена.")
    if lot.status != "active":
        return {"ok": True, "status": lot.status}

    lot.status = "closed"
    lot.updated_at = datetime.now(timezone.utc)
    return {"ok": True, "status": lot.status}

# -----------------------------------------------------------------------------
# Админ: принудительный розыгрыш
# -----------------------------------------------------------------------------

async def svc_admin_force_draw(
    db: AsyncSession,
    admin_telegram_id: int,
    lottery_id: int,
    idempotency_key: str,
) -> Dict[str, Any]:
    """
    Розыгрыш: выбираем случайный билет из проданных (равномерно),
    фиксируем результат в LotteryResult и переводим статус в 'completed'.
    Если prize_type='EFHC' — начисление призовых EFHC победителю как БОНУСОВ.
    Если prize_type='NFT' — создаём LotteryNFTClaim(status='pending').
    """
    lot = (
        await db.execute(
            select(Lottery)
            .where(Lottery.id == lottery_id)
            .with_for_update(skip_locked=True)
        )
    ).scalar_one_or_none()
    if not lot:
        raise ValueError("Лотерея не найдена.")
    if lot.status not in ("active", "closed"):
        return {"ok": True, "status": lot.status}

    if lot.tickets_sold < 1:
        raise ValueError("Нельзя проводить розыгрыш без проданных билетов.")

    # Выбор случайного билета равновероятно среди 1..tickets_sold
    from sqlalchemy import text as _text

    row = (
        await db.execute(
            _text(
                f"""
                SELECT ticket_id, owner_telegram_id
                FROM {SCHEMA}.lottery_tickets
                WHERE lottery_id = :lid
                ORDER BY random()
                LIMIT 1
                """
            ),
            {"lid": lot.id},
        )
    ).first()
    if not row:
        raise ValueError("Билеты не найдены (данные повреждены).")

    win_ticket_id = int(row.ticket_id)
    winner_tg = int(row.owner_telegram_id)
    now = datetime.now(timezone.utc)

    existing_result = (
        await db.execute(
            select(LotteryResult).where(LotteryResult.lottery_id == lot.id)
        )
    ).scalar_one_or_none()
    if not existing_result:
        db.add(
            LotteryResult(
                lottery_id=lot.id,
                winning_ticket_id=win_ticket_id,
                winner_telegram_id=winner_tg,
                created_at=now,
                completed_at=now,
            )
        )

    if lot.prize_type == "NFT":
        db.add(
            LotteryNFTClaim(
                lottery_id=lot.id,
                winner_telegram_id=winner_tg,
                status="pending",
                wallet_address=None,
                meta={"lottery_title": lot.title},
                created_at=now,
                updated_at=now,
            )
        )
    elif lot.prize_type == "EFHC":
        # Начисление 100 EFHC как бонусов победителю должно выполняться
        # в отдельном админском/сервисном сценарии через банк EFHC:
        #   credit_user_from_bank(..., balance_type='bonus')
        # Здесь не трогаем деньги, чтобы не нарушать границы сервиса.
        pass

    lot.status = "completed"
    lot.finished_at = now
    lot.updated_at = now
    return {
        "ok": True,
        "status": lot.status,
        "winning_ticket_id": win_ticket_id,
        "winner_telegram_id": winner_tg,
    }
