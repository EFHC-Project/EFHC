# -*- coding: utf-8 -*-
# backend/app/services/admin/admin_bank_service.py
# =============================================================================
# EFHC Bot — Банк EFHC: начальная эмиссия, ручные операции Банк↔Пользователь,
# логирование и безопасные откаты через компенсирующие транзакции
# -----------------------------------------------------------------------------
# Назначение модуля:
#   • Централизованный сервис для операций с Банком EFHC на уровне админ-панели.
#   • Строгий канон: НЕТ P2P-переводов между пользователями, только:
#         Банк → Пользователь
#         Пользователь → Банк
#   • Начальная эмиссия Банка: 5 000 000 EFHC (идемпотентная инициализация).
#   • Ручные корректировки:
#       - обычный EFHC (regular/main),
#       - бонусный EFHC (bonus).
#   • Полная идемпотентность:
#       - mint/burn по idempotency_key,
#       - ручные корректировки по idempotency_key,
#       - откаты через компенсирующие операции.
#   • Логи:
#       - агрегированный баланс Банка (bank_balances),
#       - история Банк↔Пользователь (internal_tx),
#       - действия админов (admin_logs),
#       - отдельная история mint/burn (efhc_mint_burn).
#
# ВАЖНО (канон):
#   1) Начальный баланс Банка — 5 000 000 EFHC (может быть переопределён в .env,
#      но дефолт всегда 5 000 000). Инициализация должна быть ИДЕМПОТЕНТНОЙ.
#   2) Переводы между пользователями НАПРЯМУЮ запрещены. Любая корректировка:
#         userA → Банк → userB
#      через две отдельные операции, а не через P2P.
#   3) Все ручные транзакции админа Банк↔Пользователь проходят только через
#      банковский сервис transactions_service.* (credit/debit, включая бонусный
#      баланс). Никаких прямых UPDATE балансов пользователей в этом модуле.
#   4) Все операции имеют idempotency_key, который должен приходить с клиента
#      (админ-панель). Модуль не генерирует ключи сам, чтобы не ломать канон.
#   5) Все бонусные начисления (реферальные, задания, лотереи и др.) должны
#      попадать только в бонусный баланс (bonus_balance / bonus-ветка в банке).
#      В этом модуле — строгий выбор balance_type.
# =============================================================================

from __future__ import annotations

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

from backend.app.services.transactions_service import (
    credit_user_from_bank,
    credit_user_bonus_from_bank,
    debit_user_to_bank,
    debit_user_bonus_to_bank,
)

from .admin_rbac import AdminUser, AdminRole, RBAC
from .admin_logging import AdminLogger
from .admin_notifications import AdminNotifier

logger = get_logger(__name__)
S = get_settings()

SCHEMA_ADMIN: str = getattr(S, "DB_SCHEMA_ADMIN", "efhc_admin") or "efhc_admin"
SCHEMA_CORE: str = getattr(S, "DB_SCHEMA_CORE", "efhc_core") or "efhc_core"

# Telegram-ID «банковского» аккаунта (логический идентификатор Банка в internal_tx)
BANK_TG_ID: int = int(getattr(S, "BANK_TELEGRAM_ID", 0) or 0)

# Начальная эмиссия Банка (канон: 5 000 000 EFHC, можно переопределить в .env)
BANK_INITIAL_TOTAL: Decimal = Decimal(
    str(getattr(S, "BANK_INITIAL_TOTAL_EFHC", "5000000") or "5000000")
)

# Универсальная точность EFHC (по канону 8 знаков)
EFHC_DECIMALS: int = int(getattr(S, "EFHC_DECIMALS", 8) or 8)
_Q = Decimal(1).scaleb(-EFHC_DECIMALS)


def d8(x: Any) -> Decimal:
    """Округление вниз до EFHC_DECIMALS знаков (канон для EFHC)."""
    return Decimal(str(x)).quantize(_Q, rounding=ROUND_DOWN)


if BANK_TG_ID <= 0:
    logger.warning(
        "BANK_TELEGRAM_ID не задан или <= 0. "
        "Логи internal_tx по Банку могут быть неполными. Настройте .env."
    )

# =============================================================================
# DTO / запросы / ответы для админки
# =============================================================================


class MintBurnRequest(BaseModel):
    """
    Запрос на минт/бёрн EFHC в банке.

    amount          — сколько EFHC минтить/сжечь (> 0, с округлением до 8 знаков).
    reason          — текстовая причина (видна только в админ-логах).
    idempotency_key — обязательный ключ идемпотентности (уникален для операции).
    """

    amount: Decimal = Field(..., gt=Decimal("0"))
    reason: Optional[str] = None
    idempotency_key: str = Field(..., min_length=1, max_length=128)

    @validator("amount", pre=True)
    def _v_amount(cls, v: Any) -> Decimal:
        return quantize_decimal(v, EFHC_DECIMALS, "DOWN")

    @validator("idempotency_key")
    def _v_idem(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("idempotency_key обязателен")
        return v


class BankUserTransferRequest(BaseModel):
    """
    Ручная операция между Банком и пользователем (корректировка админом).

    user_id         — ID пользователя в системе.
    amount          — сумма EFHC (> 0, округление вниз).
    direction       — направление:
                        'BANK_TO_USER'  (кредит пользователю из Банка),
                        'USER_TO_BANK'  (дебет пользователя в Банк).
    balance_type    — тип баланса:
                        'REGULAR' — обычный EFHC (main_balance),
                        'BONUS'   — бонусный EFHC (bonus_balance).
    reason          — причина (для логов/аудита).
    idempotency_key — ключ идемпотентности (обязателен).
    """

    user_id: int = Field(..., ge=1)
    amount: Decimal = Field(..., gt=Decimal("0"))
    direction: Literal["BANK_TO_USER", "USER_TO_BANK"]
    balance_type: Literal["REGULAR", "BONUS"]
    reason: Optional[str] = None
    idempotency_key: str = Field(..., min_length=1, max_length=128)

    @validator("amount", pre=True)
    def _v_amount(cls, v: Any) -> Decimal:
        return quantize_decimal(v, EFHC_DECIMALS, "DOWN")

    @validator("idempotency_key")
    def _v_idem(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("idempotency_key обязателен")
        return v


class BankBalance(BaseModel):
    """Сводный баланс Банка EFHC (согласно таблице bank_balances)."""

    balance: str  # строка с 8 знаками после запятой
    telegram_id: int


class BankTransferLogEntry(BaseModel):
    """
    Строка истории Банк↔Пользователь (из internal_tx), отфильтрованная так,
    чтобы хотя бы одна сторона была BANK_TG_ID.

    ВНИМАНИЕ: здесь только лог. Реальная экономика — в transactions_service.
    """

    id: int
    from_user_id: Optional[int]
    to_user_id: Optional[int]
    amount: str
    reason: str
    created_at: str


# =============================================================================
# Сервис Банка EFHC
# =============================================================================


class BankService:
    """
    Сервис банковских операций для админ-панели.

    Внутренние инварианты:
      • НЕТ P2P: модуль не предоставляет функций user→user.
      • Все операции с пользователями делегируются в transactions_service:
            credit_user_from_bank(...)
            credit_user_bonus_from_bank(...)
            debit_user_to_bank(...)
            debit_user_bonus_to_bank(...)
      • Все публичные методы, которые меняют состояние, требуют:
            - AdminUser с достаточной ролью (RBAC),
            - idempotency_key (для устойчивости и повторов).
    """

    # -------------------------------------------------------------------------
    # Инициализация Банка (начальный баланс 5 000 000 EFHC)
    # -------------------------------------------------------------------------

    @staticmethod
    async def ensure_initial_balance(db: AsyncSession) -> Dict[str, Any]:
        """
        Идемпотентно инициализирует запись Банка в {SCHEMA_ADMIN}.bank_balances.

        Логика:
          • Если для BANK_TG_ID уже есть строка — ничего не делаем.
          • Если строки нет — создаём с балансом BANK_INITIAL_TOTAL.

        Важно:
          • Эта функция НЕ пишет в efhc_mint_burn (начальная эмиссия считается
            «системной»). При желании можно записать туда отдельной миграцией.
        """
        if BANK_TG_ID <= 0:
            raise ValueError("BANK_TELEGRAM_ID не настроен (<=0). Невозможно инициализировать Банк.")

        sql_check = text(
            f"""
            SELECT efhc_balance
            FROM {SCHEMA_ADMIN}.bank_balances
            WHERE telegram_id = :tid
            LIMIT 1
            """
        )
        r: Result = await db.execute(sql_check, {"tid": BANK_TG_ID})
        row = r.fetchone()
        if row:
            current = Decimal(str(row.efhc_balance or "0"))
            return {
                "initialized": False,
                "balance_before": format_decimal_str(current, EFHC_DECIMALS),
                "balance_after": format_decimal_str(current, EFHC_DECIMALS),
            }

        # Создаём запись с начальным балансом
        init_amt = d8(BANK_INITIAL_TOTAL)
        await db.execute(
            text(
                f"""
                INSERT INTO {SCHEMA_ADMIN}.bank_balances (telegram_id, efhc_balance)
                VALUES (:tid, :amt)
                """
            ),
            {"tid": BANK_TG_ID, "amt": str(init_amt)},
        )

        logger.info(
            "BankService.ensure_initial_balance: создана запись Банка с балансом %s EFHC",
            format_decimal_str(init_amt, EFHC_DECIMALS),
        )

        return {
            "initialized": True,
            "balance_before": format_decimal_str(Decimal("0"), EFHC_DECIMALS),
            "balance_after": format_decimal_str(init_amt, EFHC_DECIMALS),
        }

    # -------------------------------------------------------------------------
    # Текущий баланс Банка
    # -------------------------------------------------------------------------

    @staticmethod
    async def get_bank_balance(db: AsyncSession) -> BankBalance:
        """
        Возвращает текущий баланс Банка EFHC по таблице bank_balances.

        Если записи нет — возвращает 0. Это не ошибка: значит,
        ensure_initial_balance() ещё не запускали или миграция не выполнена.
        """
        if BANK_TG_ID <= 0:
            raise ValueError("BANK_TELEGRAM_ID не настроен (<=0)")

        r: Result = await db.execute(
            text(
                f"""
                SELECT efhc_balance
                FROM {SCHEMA_ADMIN}.bank_balances
                WHERE telegram_id = :tid
                LIMIT 1
                """
            ),
            {"tid": BANK_TG_ID},
        )
        row = r.fetchone()
        bal = Decimal(str(row.efhc_balance or "0")) if row else Decimal("0")
        return BankBalance(
            balance=format_decimal_str(d8(bal), EFHC_DECIMALS),
            telegram_id=BANK_TG_ID,
        )

    # -------------------------------------------------------------------------
    # Минт/бёрн EFHC (только Банк)
    # -------------------------------------------------------------------------

    @staticmethod
    async def mint_efhc(db: AsyncSession, req: MintBurnRequest, admin: AdminUser) -> Dict[str, Any]:
        """
        Минт EFHC в Банк:

          • Увеличивает efhc_balance в bank_balances для BANK_TG_ID.
          • Пишет строку в efhc_mint_burn с idempotency_key.
          • Логирует действие в admin_logs и при необходимости уведомляет админ-чат.

        RBAC:
          • Только SUPERADMIN.
        """
        RBAC.require_role(admin, AdminRole.SUPERADMIN)
        if BANK_TG_ID <= 0:
            raise ValueError("BANK_TELEGRAM_ID не настроен (<=0)")

        amt = d8(req.amount)

        # Идемпотентность: проверяем, выполнялся ли уже mint с этим ключом
        r: Result = await db.execute(
            text(
                f"""
                SELECT id, amount
                FROM {SCHEMA_ADMIN}.efhc_mint_burn
                WHERE idempotency_key = :ik AND action = 'MINT'
                LIMIT 1
                """
            ),
            {"ik": req.idempotency_key},
        )
        row = r.fetchone()
        if row:
            # Уже зафиксированный mint — считаем операцию повторной, но безопасной
            logger.info(
                "BankService.mint_efhc: повторный запрос с тем же idempotency_key=%s, amount=%s",
                req.idempotency_key,
                row.amount,
            )
            # Баланс мог измениться другими операциями → просто возвращаем текущий
            bal = await BankService.get_bank_balance(db)
            return {
                "ok": True,
                "minted": format_decimal_str(d8(row.amount), EFHC_DECIMALS),
                "idempotency_key": req.idempotency_key,
                "current_balance": bal.balance,
                "reused": True,
            }

        # Обновление банка
        await db.execute(
            text(
                f"""
                INSERT INTO {SCHEMA_ADMIN}.bank_balances (telegram_id, efhc_balance)
                VALUES (:tid, :amt)
                ON CONFLICT (telegram_id) DO UPDATE
                SET efhc_balance = {SCHEMA_ADMIN}.bank_balances.efhc_balance + :amt
                """
            ),
            {"tid": BANK_TG_ID, "amt": str(amt)},
        )

        # Лог в efhc_mint_burn
        await db.execute(
            text(
                f"""
                INSERT INTO {SCHEMA_ADMIN}.efhc_mint_burn
                    (action, amount, admin_id, reason, idempotency_key, created_at)
                VALUES
                    ('MINT', :amt, :aid, :reason, :ik, NOW() AT TIME ZONE 'UTC')
                """
            ),
            {
                "amt": str(amt),
                "aid": admin.id,
                "reason": req.reason or "",
                "ik": req.idempotency_key,
            },
        )

        # Лог действий админа
        await AdminLogger.write(
            db,
            admin_id=admin.id,
            action="BANK_MINT",
            entity="bank",
            entity_id=None,
            details=f"amount={format_decimal_str(amt, EFHC_DECIMALS)}; ik={req.idempotency_key}",
        )

        # Уведомление (опционально)
        await AdminNotifier.notify_generic(
            db,
            event="BANK_MINT",
            message=f"Минт {format_decimal_str(amt, EFHC_DECIMALS)} EFHC",
            payload_json=f'{{"amount":"{format_decimal_str(amt, EFHC_DECIMALS)}","admin_id":{admin.id}}}',
        )

        bal = await BankService.get_bank_balance(db)
        return {
            "ok": True,
            "minted": format_decimal_str(amt, EFHC_DECIMALS),
            "idempotency_key": req.idempotency_key,
            "current_balance": bal.balance,
            "reused": False,
        }

    @staticmethod
    async def burn_efhc(db: AsyncSession, req: MintBurnRequest, admin: AdminUser) -> Dict[str, Any]:
        """
        Бёрн EFHC из Банка:

          • Уменьшает efhc_balance в bank_balances для BANK_TG_ID.
          • Не допускает «сжигание в минус» — количество EFHC в Банке
            должно быть ≥ amount.
          • Логирует операцию и сохраняет idempotency_key.

        RBAC:
          • Только SUPERADMIN.
        """
        RBAC.require_role(admin, AdminRole.SUPERADMIN)
        if BANK_TG_ID <= 0:
            raise ValueError("BANK_TELEGRAM_ID не настроен (<=0)")

        amt = d8(req.amount)

        # Идемпотентность: бёрн с тем же ключом не должен повторяться
        r: Result = await db.execute(
            text(
                f"""
                SELECT id, amount
                FROM {SCHEMA_ADMIN}.efhc_mint_burn
                WHERE idempotency_key = :ik AND action = 'BURN'
                LIMIT 1
                """
            ),
            {"ik": req.idempotency_key},
        )
        row = r.fetchone()
        if row:
            logger.info(
                "BankService.burn_efhc: повторный запрос с тем же idempotency_key=%s, amount=%s",
                req.idempotency_key,
                row.amount,
            )
            bal = await BankService.get_bank_balance(db)
            return {
                "ok": True,
                "burned": format_decimal_str(d8(row.amount), EFHC_DECIMALS),
                "idempotency_key": req.idempotency_key,
                "current_balance": bal.balance,
                "reused": True,
            }

        # Проверяем остаток
        r2: Result = await db.execute(
            text(
                f"""
                SELECT efhc_balance
                FROM {SCHEMA_ADMIN}.bank_balances
                WHERE telegram_id = :tid
                LIMIT 1
                """
            ),
            {"tid": BANK_TG_ID},
        )
        row2 = r2.fetchone()
        cur = Decimal(str(row2.efhc_balance or "0")) if row2 else Decimal("0")
        if cur < amt:
            raise ValueError("Недостаточно EFHC в банке для сжигания")

        # Списываем
        await db.execute(
            text(
                f"""
                UPDATE {SCHEMA_ADMIN}.bank_balances
                SET efhc_balance = efhc_balance - :amt
                WHERE telegram_id = :tid
                """
            ),
            {"tid": BANK_TG_ID, "amt": str(amt)},
        )

        # Лог в efhc_mint_burn
        await db.execute(
            text(
                f"""
                INSERT INTO {SCHEMA_ADMIN}.efhc_mint_burn
                    (action, amount, admin_id, reason, idempotency_key, created_at)
                VALUES
                    ('BURN', :amt, :aid, :reason, :ik, NOW() AT TIME ZONE 'UTC')
                """
            ),
            {
                "amt": str(amt),
                "aid": admin.id,
                "reason": req.reason or "",
                "ik": req.idempotency_key,
            },
        )

        await AdminLogger.write(
            db,
            admin_id=admin.id,
            action="BANK_BURN",
            entity="bank",
            entity_id=None,
            details=f"amount={format_decimal_str(amt, EFHC_DECIMALS)}; ik={req.idempotency_key}",
        )

        await AdminNotifier.notify_generic(
            db,
            event="BANK_BURN",
            message=f"Сжигание {format_decimal_str(amt, EFHC_DECIMALS)} EFHC",
            payload_json=f'{{"amount":"{format_decimal_str(amt, EFHC_DECIMALS)}","admin_id":{admin.id}}}',
        )

        bal = await BankService.get_bank_balance(db)
        return {
            "ok": True,
            "burned": format_decimal_str(amt, EFHC_DECIMALS),
            "idempotency_key": req.idempotency_key,
            "current_balance": bal.balance,
            "reused": False,
        }

    # -------------------------------------------------------------------------
    # Ручные операции Банк↔Пользователь (только через transactions_service)
    # -------------------------------------------------------------------------

    @staticmethod
    async def manual_bank_user_transfer(
        db: AsyncSession,
        req: BankUserTransferRequest,
        admin: AdminUser,
    ) -> Dict[str, Any]:
        """
        Выполняет ручную корректировку между Банком и пользователем.

        Канон:
          • direction:
              - 'BANK_TO_USER' — кредит пользователю из Банка.
              - 'USER_TO_BANK' — дебет пользователя в Банк.
          • balance_type:
              - 'REGULAR' — обычный EFHC.
              - 'BONUS'   — бонусный EFHC (строгий канон для бонусов).
          • Внутренне всегда вызывается только банковский сервис:
              credit_user_from_bank / credit_user_bonus_from_bank /
              debit_user_to_bank / debit_user_bonus_to_bank.

        RBAC:
          • Минимум MODERATOR.
        """
        RBAC.require_role(admin, AdminRole.MODERATOR)

        amt = d8(req.amount)
        if amt <= 0:
            raise ValueError("Сумма должна быть больше нуля")

        # Общая причина/мета для банковского лога
        base_reason = req.reason or "ADMIN_MANUAL"
        meta = {
            "admin_id": admin.id,
            "admin_role": admin.role,
            "admin_reason": base_reason,
            "source": "admin_manual_bank_transfer",
        }

        try:
            if req.direction == "BANK_TO_USER":
                # Банк → Пользователь
                if req.balance_type == "REGULAR":
                    await credit_user_from_bank(
                        db,
                        user_id=req.user_id,
                        amount=amt,
                        reason="admin_manual_regular_credit",
                        idempotency_key=req.idempotency_key,
                        meta=meta,
                    )
                    action = "BANK_TO_USER_REGULAR"
                else:
                    await credit_user_bonus_from_bank(
                        db,
                        user_id=req.user_id,
                        amount=amt,
                        reason="admin_manual_bonus_credit",
                        idempotency_key=req.idempotency_key,
                        meta=meta,
                    )
                    action = "BANK_TO_USER_BONUS"
            else:
                # Пользователь → Банк
                if req.balance_type == "REGULAR":
                    await debit_user_to_bank(
                        db,
                        user_id=req.user_id,
                        amount=amt,
                        reason="admin_manual_regular_debit",
                        idempotency_key=req.idempotency_key,
                        meta=meta,
                        spend_bonus_first=False,
                        forbid_user_negative=True,
                    )
                    action = "USER_TO_BANK_REGULAR"
                else:
                    await debit_user_bonus_to_bank(
                        db,
                        user_id=req.user_id,
                        amount=amt,
                        reason="admin_manual_bonus_debit",
                        idempotency_key=req.idempotency_key,
                        meta=meta,
                        forbid_user_negative=True,
                    )
                    action = "USER_TO_BANK_BONUS"

        except Exception as e:
            # Перехватываем любые ошибки и транслируем в понятную для UI
            logger.warning(
                "BankService.manual_bank_user_transfer failed (user=%s, dir=%s, bal=%s): %s",
                req.user_id,
                req.direction,
                req.balance_type,
                e,
            )
            raise ValueError(f"Не удалось выполнить операцию: {type(e).__name__}: {e}") from e

        # Лог админа (отдельно от банковского лога)
        await AdminLogger.write(
            db,
            admin_id=admin.id,
            action=action,
            entity="bank_user_tx",
            entity_id=req.user_id,
            details=f"amount={format_decimal_str(amt, EFHC_DECIMALS)}; ik={req.idempotency_key}; balance_type={req.balance_type}",
        )

        # Уведомление (опционально)
        await AdminNotifier.notify_generic(
            db,
            event=action,
            message=(
                f"{'Кредит' if req.direction=='BANK_TO_USER' else 'Дебет'} "
                f"{format_decimal_str(amt, EFHC_DECIMALS)} EFHC "
                f"({req.balance_type}) пользователю {req.user_id}"
            ),
            payload_json=(
                f'{{"user_id":{req.user_id},"amount":"{format_decimal_str(amt, EFHC_DECIMALS)}",'
                f'"direction":"{req.direction}","balance_type":"{req.balance_type}",'
                f'"admin_id":{admin.id},"ik":"{req.idempotency_key}"}}'
            ),
        )

        return {
            "ok": True,
            "user_id": req.user_id,
            "amount": format_decimal_str(amt, EFHC_DECIMALS),
            "direction": req.direction,
            "balance_type": req.balance_type,
            "idempotency_key": req.idempotency_key,
        }

    # -------------------------------------------------------------------------
    # История операций Банк↔Пользователь
    # -------------------------------------------------------------------------

    @staticmethod
    async def list_bank_user_transfers(
        db: AsyncSession,
        *,
        user_id: Optional[int] = None,
        limit: int = 100,
        offset: int = 0,
        sort_desc: bool = True,
    ) -> List[BankTransferLogEntry]:
        """
        Возвращает историю внутренних переводов, где участвует Банк:

          • from_user_id = BANK_TG_ID или to_user_id = BANK_TG_ID.
          • При user_id — фильтр по конкретному пользователю.
          • Таблица: {SCHEMA_CORE}.internal_tx.

        Внимание:
          • Это лог (для просмотра/аудита), а не средство изменения данных.
        """
        if BANK_TG_ID <= 0:
            raise ValueError("BANK_TELEGRAM_ID не настроен (<=0)")

        limit = max(1, min(limit, 500))
        offset = max(0, offset)
        order = "DESC" if sort_desc else "ASC"

        where = [
            "(from_user_id = :bank_id OR to_user_id = :bank_id)",
        ]
        params: Dict[str, Any] = {
            "bank_id": BANK_TG_ID,
            "limit": limit,
            "offset": offset,
        }
        if user_id is not None:
            where.append("(from_user_id = :uid OR to_user_id = :uid)")
            params["uid"] = user_id

        sql = text(
            f"""
            SELECT id, from_user_id, to_user_id, amount, reason, created_at
            FROM {SCHEMA_CORE}.internal_tx
            WHERE {" AND ".join(where)}
            ORDER BY id {order}
            LIMIT :limit OFFSET :offset
            """
        )
        r: Result = await db.execute(sql, params)
        out: List[BankTransferLogEntry] = []
        for row in r.fetchall():
            out.append(
                BankTransferLogEntry(
                    id=int(row.id),
                    from_user_id=int(row.from_user_id) if row.from_user_id is not None else None,
                    to_user_id=int(row.to_user_id) if row.to_user_id is not None else None,
                    amount=str(row.amount),
                    reason=str(row.reason),
                    created_at=row.created_at.isoformat()
                    if hasattr(row.created_at, "isoformat")
                    else str(row.created_at),
                )
            )
        return out

    # -------------------------------------------------------------------------
    # «Откат» через компенсирующую транзакцию (мягкий rollback)
    # -------------------------------------------------------------------------

    @staticmethod
    async def rollback_bank_user_tx(
        db: AsyncSession,
        *,
        internal_tx_id: int,
        balance_type: Literal["REGULAR", "BONUS"],
        idempotency_key: str,
        admin: AdminUser,
    ) -> Dict[str, Any]:
        """
        Выполняет мягкий «откат» внутренней операции Банк↔Пользователь через
        компенсирующую транзакцию с противоположным направлением.

        Ограничения/ИИ-защита:
          • Работает только с записями internal_tx, где строго одна сторона — Банк:
                from_user_id = BANK_TG_ID, to_user_id = user
                ИЛИ
                from_user_id = user, to_user_id = BANK_TG_ID.
          • НИКАКИХ P2P внутр. переводов: если обе стороны != BANK_TG_ID,
            выбрасывается ошибка.
          • Направление отката:
                Банк → Пользователь  →   rollback: Пользователь → Банк
                Пользователь → Банк  →   rollback: Банк → Пользователь
          • balance_type нужно указать явно (регулярный или бонусный счёт), так как
            таблица internal_tx не хранит тип баланса.

        RBAC:
          • Минимум MODERATOR.
        """
        RBAC.require_role(admin, AdminRole.MODERATOR)
        if BANK_TG_ID <= 0:
            raise ValueError("BANK_TELEGRAM_ID не настроен (<=0)")

        if not idempotency_key.strip():
            raise ValueError("idempotency_key обязателен для отката")

        # Ищем исходную операцию
        r: Result = await db.execute(
            text(
                f"""
                SELECT id, from_user_id, to_user_id, amount, reason, created_at
                FROM {SCHEMA_CORE}.internal_tx
                WHERE id = :txid
                LIMIT 1
                """
            ),
            {"txid": internal_tx_id},
        )
        row = r.fetchone()
        if not row:
            raise ValueError("Оригинальная транзакция не найдена")

        from_uid = int(row.from_user_id) if row.from_user_id is not None else None
        to_uid = int(row.to_user_id) if row.to_user_id is not None else None
        amount = d8(row.amount)
        original_reason = str(row.reason)

        # Проверяем, что это Банк↔Пользователь
        if from_uid == BANK_TG_ID and to_uid and to_uid != BANK_TG_ID:
            user_id = to_uid
            direction = "BANK_TO_USER"
        elif to_uid == BANK_TG_ID and from_uid and from_uid != BANK_TG_ID:
            user_id = from_uid
            direction = "USER_TO_BANK"
        else:
            # либо P2P, либо обе стороны Банк — не трогаем
            raise ValueError("Откат поддерживается только для операций Банк↔Пользователь")

        # Настраиваем компенсирующее направление
        if direction == "BANK_TO_USER":
            # Оригинал: Банк → Пользователь; откат: Пользователь → Банк
            rollback_req = BankUserTransferRequest(
                user_id=user_id,
                amount=amount,
                direction="USER_TO_BANK",
                balance_type=balance_type,
                reason=f"ROLLBACK:{internal_tx_id}|{original_reason}",
                idempotency_key=idempotency_key,
            )
        else:
            # Оригинал: Пользователь → Банк; откат: Банк → Пользователь
            rollback_req = BankUserTransferRequest(
                user_id=user_id,
                amount=amount,
                direction="BANK_TO_USER",
                balance_type=balance_type,
                reason=f"ROLLBACK:{internal_tx_id}|{original_reason}",
                idempotency_key=idempotency_key,
            )

        # Выполняем компенсирующую операцию
        result = await BankService.manual_bank_user_transfer(
            db,
            req=rollback_req,
            admin=admin,
        )

        # Дополнительный лог для явного помечания отката
        await AdminLogger.write(
            db,
            admin_id=admin.id,
            action="BANK_ROLLBACK",
            entity="bank_user_tx",
            entity_id=user_id,
            details=f"rollback_tx_id={internal_tx_id}; balance_type={balance_type}; ik={idempotency_key}",
        )

        return {
            "ok": True,
            "rollback_of": internal_tx_id,
            "compensation": result,
        }


__all__ = [
    "MintBurnRequest",
    "BankUserTransferRequest",
    "BankBalance",
    "BankTransferLogEntry",
    "BankService",
]

