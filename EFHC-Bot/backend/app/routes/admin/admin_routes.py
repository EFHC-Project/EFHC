# -*- coding: utf-8 -*-
# backend/app/routes/admin_routes.py
# =============================================================================
# Назначение кода:
# Админ-API для управляемости EFHC Bot: корректировки балансов «банк↔пользователь»,
# ручная реконсиляция входящих TON-платежей, сводные отчёты и тех.диагностика.
#
# Канон/инварианты (важно):
# • Любые денежные операции идут ТОЛЬКО через банковский сервис transactions_service.
# • Денежные POST требуют заголовок Idempotency-Key (строгая идемпотентность).
# • Пользователи не могут уходить в минус (жёсткое правило на уровне сервисов/БД).
# • Банк может быть в минусе — это не блокирует операции (фиксируется в логах).
# • Вход в админку: require_admin_or_nft_or_key (ID админа ИЛИ наличие админ-NFT ИЛИ админ-ключ).
#
# ИИ-защита/самовосстановление:
# • Отсутствие второстепенных модулей (reports_service и т.п.) не валит приложение —
#   возвращаем дружелюбный отчёт и рекомендации, продолжая обслуживание.
# • Все денежные вызовы — read-through идемпотентные (по idempotency_key внутри сервисов).
# • «Принудительная синхронизация»: ручные эндпоинты реконсиляции TON читают «хвосты»
#   и закрывают дыры без остановки цикла планировщика.
#
# Запреты:
# • Никаких прямых SQL для денег из роутов; только сервисные функции.
# • Никаких P2P переводов user→user; любые корректировки — исключительно «пользователь↔банк».
# =============================================================================

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Header, Query, status
from pydantic import BaseModel, Field, conint, constr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.logging_core import get_logger
from backend.app.core.security_core import require_admin_or_nft_or_key  # централизованный мини-Depends
from backend.app.deps import get_db  # сессия БД (AsyncSession), d8/etag/cursor живут тут
from backend.app.deps import d8

# Банковские операции (read-through идемпотентность внутри сервисов)
from backend.app.services.transactions_service import (
    credit_user_main_from_bank,
    credit_user_bonus_from_bank,
    debit_user_main_to_bank,
    debit_user_bonus_to_bank,
)

# Реконсиляция TON
from backend.app.services.watcher_service import (
    process_incoming_payments,   # async def process_incoming_payments(db, limit: int, since_utime: Optional[int]) -> Dict
    reconcile_last_hours,        # async def reconcile_last_hours(db, hours: int) -> Dict
)

logger = get_logger(__name__)
router = APIRouter(prefix="/admin", tags=["admin"])

# =============================================================================
# Модели запросов/ответов
# =============================================================================

class AdminTransferIn(BaseModel):
    """
    Корректировка баланса через «банк↔пользователь».
    Одно из полей идентификации обязательно: user_id ИЛИ telegram_id.
    direction: 'bank_to_user' | 'user_to_bank'
    balance:   'main' | 'bonus'  — какой счёт пользователя задействуем.
    """
    user_id: Optional[int] = Field(None, description="Внутренний ID пользователя")
    telegram_id: Optional[int] = Field(None, description="Telegram ID пользователя")
    direction: constr(strip_whitespace=True, pattern="^(bank_to_user|user_to_bank)$")
    balance: constr(strip_whitespace=True, pattern="^(main|bonus)$") = "main"
    amount: Decimal = Field(..., description="Сумма EFHC (>= 0)")
    reason: Optional[constr(strip_whitespace=True, max_length=255)] = Field(
        None, description="Текстовая причина/комментарий (логируется)"
    )

class AdminTransferOut(BaseModel):
    ok: bool
    user_id: int
    telegram_id: int
    balance: str
    direction: str
    amount: str
    bank_balance_after: Optional[str] = None
    user_main_after: Optional[str] = None
    user_bonus_after: Optional[str] = None
    idempotency_key: str

class TonReconcileIn(BaseModel):
    """
    Реконсиляция входящих TON согласно одному из режимов:
    • since_utime: обрабатывать начиная с конкретного unixtime (сек), до «сейчас».
    • last_hours:  догон последних N часов (окно).
    limit — ограничение количества событий за цикл для щадящего режима.
    """
    since_utime: Optional[conint(ge=0)] = None
    last_hours: Optional[conint(ge=1, le=168)] = 6
    limit: conint(ge=1, le=5000) = 1000

class TonReconcileOut(BaseModel):
    ok: bool
    processed: int
    parsed: int
    credited: int
    duplicates: int
    errors: int
    note: Optional[str] = None

class ReportsOverviewOut(BaseModel):
    """
    Короткая сводка для админ-дашборда.
    Если таблиц ещё нет, вернём нули и подсказку «инициализируйте миграции».
    """
    ok: bool
    ts: datetime
    counters: Dict[str, int]
    notes: Optional[str] = None


# =============================================================================
# Хелпер идентификации пользователя (user_id/telegram_id)
# =============================================================================

async def _resolve_user_ids(db: AsyncSession, *, user_id: Optional[int], telegram_id: Optional[int]) -> Dict[str, int]:
    """
    Возвращает {'user_id': int, 'telegram_id': int} или бросает 400/404.
    """
    if not user_id and not telegram_id:
        raise HTTPException(status_code=400, detail="Нужно указать user_id или telegram_id.")
    if user_id and telegram_id:
        # Жёсткая однозначность — чтобы не было расхождений
        raise HTTPException(status_code=400, detail="Укажите ТОЛЬКО одно поле: user_id ИЛИ telegram_id.")

    if user_id:
        row = await db.execute(
            text("select id, telegram_id from efhc_core.users where id = :id limit 1"),
            {"id": int(user_id)},
        )
    else:
        row = await db.execute(
            text("select id, telegram_id from efhc_core.users where telegram_id = :tg limit 1"),
            {"tg": int(telegram_id)},
        )

    rec = row.fetchone()
    if not rec:
        raise HTTPException(status_code=404, detail="Пользователь не найден.")
    return {"user_id": int(rec[0]), "telegram_id": int(rec[1])}


# =============================================================================
# Денежные корректировки «банк↔пользователь»
# =============================================================================

@router.post(
    "/transfer",
    response_model=AdminTransferOut,
    summary="Корректировка «банк↔пользователь» (только через банк, read-through идемпотентность)",
)
async def admin_transfer(
    payload: AdminTransferIn,
    db: AsyncSession = Depends(get_db),
    _auth: Any = Depends(require_admin_or_nft_or_key),
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
) -> AdminTransferOut:
    """
    Что делает:
      • По direction и balance вызывает нужную банковскую операцию.
      • Идемпотентность на уровне сервиса по idempotency_key (read-through).
    Исключения:
      • 400 — отсутствие идентификатора пользователя; отриц. суммы; некорректные поля.
      • 409 — конфликт идемпотентности (в сервисе может конвертироваться в успешный read-through).
    """
    if payload.amount is None or Decimal(payload.amount) < Decimal("0"):
        raise HTTPException(status_code=400, detail="Сумма должна быть ≥ 0.")

    ident = await _resolve_user_ids(db, user_id=payload.user_id, telegram_id=payload.telegram_id)
    amount_q8 = d8(payload.amount)

    # Выполняем через банковский сервис (ровно один публичный вход)
    try:
        if payload.direction == "bank_to_user":
            if payload.balance == "main":
                res = await credit_user_main_from_bank(
                    db=db,
                    user_id=ident["user_id"],
                    amount=amount_q8,
                    reason=payload.reason or "ADMIN_CORRECTION_MAIN",
                    idempotency_key=idempotency_key,
                )
            else:
                res = await credit_user_bonus_from_bank(
                    db=db,
                    user_id=ident["user_id"],
                    amount=amount_q8,
                    reason=payload.reason or "ADMIN_CORRECTION_BONUS",
                    idempotency_key=idempotency_key,
                )
        else:  # user_to_bank
            if payload.balance == "main":
                res = await debit_user_main_to_bank(
                    db=db,
                    user_id=ident["user_id"],
                    amount=amount_q8,
                    reason=payload.reason or "ADMIN_DEBIT_MAIN_TO_BANK",
                    idempotency_key=idempotency_key,
                )
            else:
                res = await debit_user_bonus_to_bank(
                    db=db,
                    user_id=ident["user_id"],
                    amount=amount_q8,
                    reason=payload.reason or "ADMIN_DEBIT_BONUS_TO_BANK",
                    idempotency_key=idempotency_key,
                )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("admin_transfer failed: %s", e)
        raise HTTPException(status_code=500, detail="Не удалось выполнить корректировку. Повторите позже.")

    # Ожидаем, что сервис вернёт актуальные остатки (или мы дочитаем их напрямую)
    # Дочитываем остатки безопасно (без падения)
    try:
        row = await db.execute(
            text(
                """
                select main_balance, bonus_balance
                from efhc_core.users
                where id = :id
                """
            ),
            {"id": ident["user_id"]},
        )
        mb, bb = row.fetchone() or (Decimal("0"), Decimal("0"))
    except Exception:
        mb, bb = Decimal("0"), Decimal("0")

    return AdminTransferOut(
        ok=True,
        user_id=ident["user_id"],
        telegram_id=ident["telegram_id"],
        balance=payload.balance,
        direction=payload.direction,
        amount=str(d8(payload.amount)),
        bank_balance_after=str(res.get("bank_balance_after")) if isinstance(res, dict) and res.get("bank_balance_after") is not None else None,
        user_main_after=str(d8(mb)),
        user_bonus_after=str(d8(bb)),
        idempotency_key=idempotency_key,
    )


# =============================================================================
# Ручная реконсиляция входящих TON
# =============================================================================

@router.post(
    "/ton/reconcile",
    response_model=TonReconcileOut,
    summary="Реконсиляция входящих TON (since_utime либо last_hours) + limit",
)
async def ton_reconcile(
    payload: TonReconcileIn,
    db: AsyncSession = Depends(get_db),
    _auth: Any = Depends(require_admin_or_nft_or_key),
) -> TonReconcileOut:
    """
    Что делает:
      • Режим 1 (since_utime): процессим входящие начиная с конкретного unixtime.
      • Режим 2 (last_hours): догоняем хвост последних N часов.
      • Всегда безопасно к повторам (tx_hash UNIQUE, read-through).
    Исключения:
      • 400 — не указан ни один режим.
      • 500 — непредвиденная ошибка интеграции/БД (логируется, цикл не падает).
    """
    if payload.since_utime is None and payload.last_hours is None:
        raise HTTPException(status_code=400, detail="Укажите since_utime или last_hours.")

    try:
        if payload.since_utime is not None:
            stats = await process_incoming_payments(db=db, limit=int(payload.limit), since_utime=int(payload.since_utime))
        else:
            stats = await reconcile_last_hours(db=db, hours=int(payload.last_hours))
    except Exception as e:
        logger.exception("ton_reconcile failed: %s", e)
        raise HTTPException(status_code=500, detail="Ошибка реконсиляции входящих TON.")

    return TonReconcileOut(
        ok=True,
        processed=int(stats.get("processed", 0)),
        parsed=int(stats.get("parsed", 0)),
        credited=int(stats.get("credited", 0)),
        duplicates=int(stats.get("duplicates", 0)),
        errors=int(stats.get("errors", 0)),
        note=stats.get("note"),
    )


# =============================================================================
# Сводные отчёты (быстрый дашборд)
# =============================================================================

@router.get(
    "/reports/overview",
    response_model=ReportsOverviewOut,
    summary="Быстрый обзор ключевых счётчиков (логов и операций)",
)
async def reports_overview(
    db: AsyncSession = Depends(get_db),
    _auth: Any = Depends(require_admin_or_nft_or_key),
) -> ReportsOverviewOut:
    """
    Что делает:
      • Возвращает набор ключевых счётчиков для дашборда админ-панели.
      • Без внешних зависимостей — прямые безопасные SELECT COUNT(*) по ядровым таблицам.
    Идемпотентность:
      • GET без побочных эффектов.
    """
    counters: Dict[str, int] = {
        "ton_inbox_total": 0,
        "ton_inbox_errors": 0,
        "efhc_transfers_total": 0,
        "efhc_transfers_deficit": 0,
        "users_total": 0,
    }
    notes: Optional[str] = None

    try:
        # Таблица входящих TON
        row = await db.execute(text("select count(*) from efhc_core.ton_inbox_logs"))
        counters["ton_inbox_total"] = int(row.scalar() or 0)

        row = await db.execute(
            text("select count(*) from efhc_core.ton_inbox_logs where status like 'error_%'")
        )
        counters["ton_inbox_errors"] = int(row.scalar() or 0)

        # Таблица переводов EFHC (лог банка)
        row = await db.execute(text("select count(*) from efhc_core.efhc_transfers_log"))
        counters["efhc_transfers_total"] = int(row.scalar() or 0)

        row = await db.execute(
            text(
                "select count(*) from efhc_core.efhc_transfers_log "
                "where extra_info like '%processed_with_deficit%'"
            )
        )
        counters["efhc_transfers_deficit"] = int(row.scalar() or 0)

        # Пользователи
        row = await db.execute(text("select count(*) from efhc_core.users"))
        counters["users_total"] = int(row.scalar() or 0)

    except Exception as e:
        # Если миграций ещё нет или таблицы отсутствуют — не валим, а даём подсказку
        logger.warning("reports_overview partial: %s", e)
        notes = "Таблицы ещё не инициализированы или миграции не применены. Проверьте alembic."

    return ReportsOverviewOut(
        ok=True,
        ts=datetime.utcnow(),
        counters=counters,
        notes=notes,
    )


# =============================================================================
# Пояснения (для разработчиков/ревью):
# • Локальные проверки админа удалены: теперь везде мини-Depends
#   `require_admin_or_nft_or_key` из core/security_core.py. Он включает доступ
#   по ID админа, по наличию админ-NFT и по админ-ключу.
# • Денежные POST ( /admin/transfer ) требуют Idempotency-Key; сами операции
#   идут через банковский сервис (credit/debit), который реализует read-through
#   идемпотентность и жёстко запрещает «минус» у пользователей.
# • /admin/ton/reconcile — «ручной догон» входящих TON. Повторы не создают
#   дублей: tx_hash UNIQUE, статусы received/parsed/credited/error_*, next_retry_at.
# • /admin/reports/overview — быстрый отчёт без внешних зависимостей; при отсутствии
#   таблиц возвращает подсказку вместо падения, сохраняя «живость» админ-панели.
# =============================================================================
