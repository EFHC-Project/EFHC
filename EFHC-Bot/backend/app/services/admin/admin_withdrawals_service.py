# -*- coding: utf-8 -*-
# backend/app/services/admin/admin_withdrawals_service.py
# =============================================================================
# EFHC Bot — Сервис выдачи EFHC и NFT (выводы, бонусы, призы)
# -----------------------------------------------------------------------------
# Назначение модуля:
#   • Управление заявками на вывод EFHC пользователями (вывод с бота наружу).
#   • Управление очередью бонусных выплат EFHC (bonus_awards).
#   • Управление заявками на выдачу NFT (prize_claims) по лотереям и магазинам.
#
# Жёсткие каноны:
#   1) НЕТ внутренних переводов между пользователями (P2P).
#      Любое движение EFHC:
#           Банк ↔ Пользователь
#      и только через банковский сервис (transactions_service).
#   2) Все бонусные начисления идут только в бонусный баланс пользователя
#      (bonus_balance / bonus ветка банка).
#   3) Все операции (выводы, бонусы, призы) ИДЕМПОТЕНТНЫ:
#      - обязательный idempotency_key (админ/система передаёт его явно),
#      - повторные вызовы с тем же ключом не создают дублей.
#   4) Для NFT-призов и бонусов есть возможность фильтрации по VIP-статусу
#      пользователя (is_vip) для приоритизации обработки.
#   5) Изменение статусов в таблицах:
#       • withdrawal_requests: PENDING → PAID/REJECTED/ERROR
#       • bonus_awards:       PENDING → PAID/REJECTED/ERROR
#       • prize_claims:       PENDING → DONE /REJECTED
#
# Где храним данные:
#   • {SCHEMA_ADMIN}.withdrawal_requests  — заявки на вывод EFHC.
#   • {SCHEMA_ADMIN}.bonus_awards        — бонусные выплаты EFHC (только бонусный счёт).
#   • {SCHEMA_ADMIN}.prize_claims        — заявки на выдачу NFT (лотереи/магазин).
#
# ВНИМАНИЕ:
#   • Финансовые изменения балансов всегда делегируются в
#       backend.app.services.transactions_service
#     и нигде в этом модуле балансы пользователей напрямую не обновляются.
# =============================================================================

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
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
    credit_user_bonus_from_bank,
    debit_user_to_bank,
)

from .admin_rbac import AdminUser, AdminRole, RBAC
from .admin_logging import AdminLogger
from .admin_notifications import AdminNotifier

logger = get_logger(__name__)
S = get_settings()

SCHEMA_ADMIN: str = getattr(S, "DB_SCHEMA_ADMIN", "efhc_admin") or "efhc_admin"
SCHEMA_CORE: str = getattr(S, "DB_SCHEMA_CORE", "efhc_core") or "efhc_core"

BANK_TG_ID: int = int(getattr(S, "BANK_TELEGRAM_ID", 0) or 0)
EFHC_DECIMALS: int = int(getattr(S, "EFHC_DECIMALS", 8) or 8)
_Q = Decimal(1).scaleb(-EFHC_DECIMALS)


def d8(x: Any) -> Decimal:
    """Округление вниз до EFHC_DECIMALS знаков (канон для EFHC)."""
    return Decimal(str(x)).quantize(_Q, rounding=ROUND_DOWN)


# =============================================================================
# Общие DTO и перечисления
# =============================================================================

class WithdrawalStatus(str):
    """Статусы заявок на вывод EFHC."""
    PENDING = "PENDING"    # создана, ждёт обработки
    PAID = "PAID"          # успешно выплачена (дебет пользователя → Банк, отправка наружу)
    REJECTED = "REJECTED"  # отклонена админом (без списания или с рефандом)
    ERROR = "ERROR"        # ошибка при обработке (нужна ручная проверка)


class BonusAwardStatus(str):
    """Статусы бонусных выплат EFHC (bonus_awards)."""
    PENDING = "PENDING"
    PAID = "PAID"
    REJECTED = "REJECTED"
    ERROR = "ERROR"


class PrizeClaimStatus(str):
    """Статусы выдачи призов (NFT/иное)."""
    PENDING = "PENDING"
    DONE = "DONE"
    REJECTED = "REJECTED"


class WithdrawalFilters(BaseModel):
    """Фильтры для списка заявок на вывод EFHC."""
    user_id: Optional[int] = None
    status: Optional[Literal["PENDING", "PAID", "REJECTED", "ERROR"]] = None
    vip_only: bool = False
    limit: int = Field(100, ge=1, le=500)
    offset: int = Field(0, ge=0)
    sort_desc: bool = True


class BonusAwardFilters(BaseModel):
    """Фильтры для списка бонусных выплат EFHC."""
    user_id: Optional[int] = None
    status: Optional[Literal["PENDING", "PAID", "REJECTED", "ERROR"]] = None
    source: Optional[str] = None
    vip_only: bool = False
    limit: int = Field(100, ge=1, le=500)
    offset: int = Field(0, ge=0)
    sort_desc: bool = True


class PrizeClaimFilters(BaseModel):
    """Фильтры для заявок на выдачу призов (NFT и др.)."""
    user_id: Optional[int] = None
    status: Optional[Literal["PENDING", "DONE", "REJECTED"]] = None
    vip_only: bool = False
    prize_type: Optional[str] = None  # например "NFT_VIP"
    limit: int = Field(100, ge=1, le=500)
    offset: int = Field(0, ge=0)
    sort_desc: bool = True


class WithdrawalRecord(BaseModel):
    """Строка списка заявок на вывод для UI админки."""
    id: int
    user_id: int
    amount_efhc: str
    status: str
    external_address: Optional[str]
    tx_hash: Optional[str]
    created_at: str
    processed_at: Optional[str]
    error_message: Optional[str]
    is_vip: bool


class BonusAwardRecord(BaseModel):
    """Строка списка бонусных выплат EFHC (bonus_awards)."""
    id: int
    user_id: int
    amount_efhc: str
    status: str
    source: str
    created_at: str
    processed_at: Optional[str]
    meta_json: Optional[str]
    is_vip: bool


class PrizeClaimRecord(BaseModel):
    """Строка списка заявок на призы (NFT и др.)."""
    id: int
    lottery_id: Optional[int]
    user_id: int
    prize_type: str
    prize_value: str
    wallet_address: Optional[str]
    status: str
    created_at: str
    processed_at: Optional[str]
    reject_reason: Optional[str]
    tx_hash: Optional[str]
    is_vip: bool


class WithdrawalApproveRequest(BaseModel):
    """
    Запрос на подтверждение/выплату заявки на вывод.

    tx_hash          — внешний tx (TON/другая сеть), можно указать позже (опционально).
    idempotency_key  — ключ идемпотентности дебета пользователя → Банк.
    """
    tx_hash: Optional[str] = None
    idempotency_key: str = Field(..., min_length=1, max_length=128)

    @validator("idempotency_key")
    def _v_idem(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("idempotency_key обязателен")
        return v


class BonusAwardProcessRequest(BaseModel):
    """
    Запрос на выплату бонуса EFHC из bonus_awards:

      idempotency_key — ключ идемпотентности начисления бонуса (Банк→Пользователь).
    """
    idempotency_key: str = Field(..., min_length=1, max_length=128)

    @validator("idempotency_key")
    def _v_idem(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("idempotency_key обязателен")
        return v


class PrizeClaimProcessRequest(BaseModel):
    """
    Запрос на пометку заявки на приз как выполненной:

      tx_hash — внешний tx, если приз выдаётся на блокчейне (NFT).
    """
    tx_hash: Optional[str] = None


class PrizeClaimRejectRequest(BaseModel):
    """Запрос на отклонение заявки на приз (с указанием причины)."""
    reason: str = Field(..., min_length=1, max_length=500)


# =============================================================================
# Сервис обработки выводов, бонусов и призов
# =============================================================================

class WithdrawalsService:
    """
    Admin-сервис для работы с очередями:

      • withdrawal_requests  — вывод EFHC пользователям наружу;
      • bonus_awards        — бонусные начисления EFHC (только бонусный баланс);
      • prize_claims        — NFT и прочие призы (лотереи/магазин).

    Инварианты:
      • НЕТ P2P-переводов: любые дебеты/кредиты идут только через
        transactions_service и только Банк↔Пользователь.
      • Все операции с изменением балансов требуют idempotency_key.
      • Любое изменение статуса логируется в admin_logs и (при необходимости)
        дублируется уведомлением в админ-чат.
    """

    # -------------------------------------------------------------------------
    # БЛОК 1. Список заявок на вывод EFHC
    # -------------------------------------------------------------------------

    @staticmethod
    async def list_withdrawals(
        db: AsyncSession,
        filters: WithdrawalFilters,
    ) -> List[WithdrawalRecord]:
        """
        Возвращает список заявок на вывод EFHC.

        Особенности:
          • Можно фильтровать по user_id, status.
          • vip_only=True — выбираем только пользователей с is_vip=TRUE.
          • Сортировка по id (DESC/ASC).
        """
        where = ["1=1"]
        params: Dict[str, Any] = {
            "limit": filters.limit,
            "offset": filters.offset,
        }

        if filters.user_id is not None:
            where.append("w.user_id = :uid")
            params["uid"] = filters.user_id
        if filters.status is not None:
            where.append("w.status = :st")
            params["st"] = filters.status
        if filters.vip_only:
            where.append("u.is_vip = TRUE")

        order = "DESC" if filters.sort_desc else "ASC"

        sql = text(
            f"""
            SELECT
                w.id,
                w.user_id,
                w.amount_efhc,
                w.status,
                w.external_address,
                w.tx_hash,
                w.created_at,
                w.processed_at,
                w.error_message,
                COALESCE(u.is_vip, FALSE) AS is_vip
            FROM {SCHEMA_ADMIN}.withdrawal_requests w
            JOIN {SCHEMA_CORE}.users u ON u.id = w.user_id
            WHERE {" AND ".join(where)}
            ORDER BY w.id {order}
            LIMIT :limit OFFSET :offset
            """
        )
        r: Result = await db.execute(sql, params)
        out: List[WithdrawalRecord] = []
        for row in r.fetchall():
            out.append(
                WithdrawalRecord(
                    id=int(row.id),
                    user_id=int(row.user_id),
                    amount_efhc=str(row.amount_efhc),
                    status=str(row.status),
                    external_address=row.external_address,
                    tx_hash=row.tx_hash,
                    created_at=row.created_at.isoformat()
                    if hasattr(row.created_at, "isoformat")
                    else str(row.created_at),
                    processed_at=row.processed_at.isoformat()
                    if getattr(row, "processed_at", None) and hasattr(row.processed_at, "isoformat")
                    else None,
                    error_message=row.error_message,
                    is_vip=bool(row.is_vip),
                )
            )
        return out

    # -------------------------------------------------------------------------
    # БЛОК 2. Обработка заявки на вывод EFHC (approve / reject)
    # -------------------------------------------------------------------------

    @staticmethod
    async def approve_withdrawal(
        db: AsyncSession,
        *,
        withdrawal_id: int,
        req: WithdrawalApproveRequest,
        admin: AdminUser,
    ) -> Dict[str, Any]:
        """
        Подтверждает и проводит заявку на вывод EFHC:

          • Проверяет статус заявки (должен быть PENDING или ERROR).
          • Делает дебет пользователя в Банк (USER→BANK) только по основному
            (regular/main) балансу через debit_user_to_bank(...).
          • Обновляет статус заявки на PAID и сохраняет tx_hash/idempotency_key.
          • В случае ошибки:
              - статус заявки → ERROR,
              - error_message заполняется,
              - исключение прокидывается наверх в «дружелюбном» виде.

        RBAC:
          • Минимум MODERATOR.
        """
        RBAC.require_role(admin, AdminRole.MODERATOR)
        if not req.idempotency_key.strip():
            raise ValueError("idempotency_key обязателен")

        # Берём заявку под кратковременный лок (FOR UPDATE), чтобы не было гонки
        r: Result = await db.execute(
            text(
                f"""
                SELECT
                    id,
                    user_id,
                    amount_efhc,
                    status,
                    request_idempotency_key,
                    processing_idempotency_key,
                    external_address,
                    tx_hash,
                    error_message
                FROM {SCHEMA_ADMIN}.withdrawal_requests
                WHERE id = :wid
                FOR UPDATE
                """
            ),
            {"wid": withdrawal_id},
        )
        row = r.fetchone()
        if not row:
            raise ValueError("Заявка на вывод не найдена")

        if row.status in (WithdrawalStatus.PAID, WithdrawalStatus.REJECTED):
            # Уже финальный статус — не трогаем, но даём предсказуемый ответ
            logger.info(
                "approve_withdrawal: withdrawal_id=%s уже в финальном статусе %s",
                withdrawal_id,
                row.status,
            )
            return {
                "ok": False,
                "status": str(row.status),
                "detail": "Заявка уже обработана",
            }

        # Проверка идемпотентности обработки: если уже есть processing_idempotency_key
        if row.processing_idempotency_key:
            if str(row.processing_idempotency_key) == req.idempotency_key:
                # Повторный вызов с тем же ключом — считаем успешным idempotent replay
                logger.info(
                    "approve_withdrawal: повтор с тем же idempotency_key=%s для withdrawal_id=%s",
                    req.idempotency_key,
                    withdrawal_id,
                )
                return {
                    "ok": True,
                    "status": str(row.status),
                    "idempotency_key": req.idempotency_key,
                    "tx_hash": row.tx_hash,
                }
            else:
                raise ValueError("Заявка уже обрабатывается с другим idempotency_key")

        user_id = int(row.user_id)
        amount = d8(row.amount_efhc)

        meta = {
            "admin_id": admin.id,
            "admin_role": admin.role,
            "withdrawal_id": withdrawal_id,
            "external_address": row.external_address,
            "source": "withdrawal_request",
        }

        # Пытаемся списать EFHC с пользователя в пользу Банка
        try:
            await debit_user_to_bank(
                db,
                user_id=user_id,
                amount=amount,
                reason="withdrawal_payout",
                idempotency_key=req.idempotency_key,
                meta=meta,
                spend_bonus_first=False,      # списываем только основной EFHC
                forbid_user_negative=True,    # пользователь не может уйти в минус
            )
        except Exception as e:
            # Списать не удалось — фиксируем ошибку в заявке
            err_text = f"{type(e).__name__}: {e}"
            logger.warning(
                "approve_withdrawal: debit_user_to_bank failed (withdrawal_id=%s, user_id=%s): %s",
                withdrawal_id,
                user_id,
                err_text,
            )
            await db.execute(
                text(
                    f"""
                    UPDATE {SCHEMA_ADMIN}.withdrawal_requests
                    SET status = 'ERROR',
                        processing_idempotency_key = :ik,
                        error_message = :err,
                        processed_at = NOW() AT TIME ZONE 'UTC'
                    WHERE id = :wid
                    """
                ),
                {
                    "ik": req.idempotency_key,
                    "err": err_text[:500],
                    "wid": withdrawal_id,
                },
            )
            await AdminLogger.write(
                db,
                admin_id=admin.id,
                action="WITHDRAWAL_ERROR",
                entity="withdrawal",
                entity_id=withdrawal_id,
                details=err_text,
            )
            raise ValueError(f"Не удалось списать EFHC с пользователя: {err_text}") from e

        # Списание успешно — помечаем заявку как PAID
        await db.execute(
            text(
                f"""
                UPDATE {SCHEMA_ADMIN}.withdrawal_requests
                SET status = 'PAID',
                    processing_idempotency_key = :ik,
                    tx_hash = COALESCE(:txh, tx_hash),
                    error_message = NULL,
                    processed_at = NOW() AT TIME ZONE 'UTC'
                WHERE id = :wid
                """
            ),
            {
                "ik": req.idempotency_key,
                "txh": req.tx_hash,
                "wid": withdrawal_id,
            },
        )

        await AdminLogger.write(
            db,
            admin_id=admin.id,
            action="WITHDRAWAL_PAID",
            entity="withdrawal",
            entity_id=withdrawal_id,
            details=f"user_id={user_id}; amount={format_decimal_str(amount, EFHC_DECIMALS)}; tx_hash={req.tx_hash or ''}",
        )

        await AdminNotifier.notify_generic(
            db,
            event="WITHDRAWAL_PAID",
            message=(
                "Выплачен вывод #"
                f"{withdrawal_id} пользователю {user_id} на сумму "
                f"{format_decimal_str(amount, EFHC_DECIMALS)} EFHC"
            ),
            payload_json=json.dumps(
                {
                    "withdrawal_id": withdrawal_id,
                    "user_id": user_id,
                    "amount": format_decimal_str(amount, EFHC_DECIMALS),
                    "tx_hash": (req.tx_hash or "").replace("\"", "\\\""),
                },
                ensure_ascii=False,
            ),
        )

        return {
            "ok": True,
            "status": WithdrawalStatus.PAID,
            "idempotency_key": req.idempotency_key,
            "tx_hash": req.tx_hash,
        }

    @staticmethod
    async def reject_withdrawal(
        db: AsyncSession,
        *,
        withdrawal_id: int,
        reason: str,
        admin: AdminUser,
    ) -> Dict[str, Any]:
        """
        Отклоняет заявку на вывод EFHC (без движения средств).

        Логика:
          • Ожидается, что списание средств ещё не производилось (заявка в
            статусе PENDING или ERROR).
          • Статус → REJECTED, error_message содержит причину.
          • Если по каналу ранее было списание (нестандартный сценарий),
            откат выполняется через отдельный механизм Банка (rollback),
            а не здесь — чтобы не создать скрытого P2P.
        """
        RBAC.require_role(admin, AdminRole.MODERATOR)

        # Обновляем только если заявка не в финальном статусе
        r: Result = await db.execute(
            text(
                f"""
                SELECT id, status
                FROM {SCHEMA_ADMIN}.withdrawal_requests
                WHERE id = :wid
                LIMIT 1
                """
            ),
            {"wid": withdrawal_id},
        )
        row = r.fetchone()
        if not row:
            raise ValueError("Заявка на вывод не найдена")

        if row.status in (WithdrawalStatus.PAID, WithdrawalStatus.REJECTED):
            return {
                "ok": False,
                "status": str(row.status),
                "detail": "Заявка уже обработана",
            }

        await db.execute(
            text(
                f"""
                UPDATE {SCHEMA_ADMIN}.withdrawal_requests
                SET status = 'REJECTED',
                    error_message = :reason,
                    processed_at = NOW() AT TIME ZONE 'UTC'
                WHERE id = :wid
                """
            ),
            {"reason": reason[:500], "wid": withdrawal_id},
        )

        await AdminLogger.write(
            db,
            admin_id=admin.id,
            action="WITHDRAWAL_REJECT",
            entity="withdrawal",
            entity_id=withdrawal_id,
            details=reason,
        )

        await AdminNotifier.notify_generic(
            db,
            event="WITHDRAWAL_REJECT",
            message=f"Отклонён вывод #{withdrawal_id}: {reason}",
        )

        return {"ok": True, "status": WithdrawalStatus.REJECTED}

    # -------------------------------------------------------------------------
    # БЛОК 3. Бонусные выплаты EFHC (bonus_awards → бонусный баланс)
    # -------------------------------------------------------------------------

    @staticmethod
    async def list_bonus_awards(
        db: AsyncSession,
        filters: BonusAwardFilters,
    ) -> List[BonusAwardRecord]:
        """
        Возвращает список bonus_awards (только бонусные EFHC).

        Можно фильтровать по:
          • user_id, status, source;
          • vip_only — только пользователи-випы.
        """
        where = ["1=1"]
        params: Dict[str, Any] = {
            "limit": filters.limit,
            "offset": filters.offset,
        }
        if filters.user_id is not None:
            where.append("b.user_id = :uid")
            params["uid"] = filters.user_id
        if filters.status is not None:
            where.append("b.status = :st")
            params["st"] = filters.status
        if filters.source is not None:
            where.append("b.source = :src")
            params["src"] = filters.source
        if filters.vip_only:
            where.append("u.is_vip = TRUE")

        order = "DESC" if filters.sort_desc else "ASC"

        sql = text(
            f"""
            SELECT
                b.id,
                b.user_id,
                b.amount,
                b.status,
                b.source,
                b.created_at,
                b.processed_at,
                b.meta_json,
                COALESCE(u.is_vip, FALSE) AS is_vip
            FROM {SCHEMA_ADMIN}.bonus_awards b
            JOIN {SCHEMA_CORE}.users u ON u.id = b.user_id
            WHERE {" AND ".join(where)}
            ORDER BY b.id {order}
            LIMIT :limit OFFSET :offset
            """
        )
        r: Result = await db.execute(sql, params)
        out: List[BonusAwardRecord] = []
        for row in r.fetchall():
            out.append(
                BonusAwardRecord(
                    id=int(row.id),
                    user_id=int(row.user_id),
                    amount_efhc=str(row.amount),
                    status=str(row.status),
                    source=str(row.source),
                    created_at=row.created_at.isoformat()
                    if hasattr(row.created_at, "isoformat")
                    else str(row.created_at),
                    processed_at=row.processed_at.isoformat()
                    if getattr(row, "processed_at", None) and hasattr(row.processed_at, "isoformat")
                    else None,
                    meta_json=str(row.meta_json) if getattr(row, "meta_json", None) is not None else None,
                    is_vip=bool(row.is_vip),
                )
            )
        return out

    @staticmethod
    async def process_bonus_award(
        db: AsyncSession,
        *,
        award_id: int,
        req: BonusAwardProcessRequest,
        admin: AdminUser,
    ) -> Dict[str, Any]:
        """
        Выплачивает бонус EFHC (только бонусный баланс) по записи bonus_awards:

          • Требует статус PENDING или ERROR.
          • Использует credit_user_bonus_from_bank(...) — Банк→Пользователь (bonus).
          • Обновляет статус bonus_awards на PAID и сохраняет idempotency_key.
          • Идемпотентен по полю processing_idempotency_key в bonus_awards.

        RBAC:
          • Минимум MODERATOR.
        """
        RBAC.require_role(admin, AdminRole.MODERATOR)
        if not req.idempotency_key.strip():
            raise ValueError("idempotency_key обязателен")

        # Читаем запись
        r: Result = await db.execute(
            text(
                f"""
                SELECT
                    id,
                    user_id,
                    amount,
                    status,
                    processing_idempotency_key,
                    source
                FROM {SCHEMA_ADMIN}.bonus_awards
                WHERE id = :aid
                FOR UPDATE
                """
            ),
            {"aid": award_id},
        )
        row = r.fetchone()
        if not row:
            raise ValueError("Бонусная запись не найдена")

        if row.status in (BonusAwardStatus.PAID, BonusAwardStatus.REJECTED):
            return {
                "ok": False,
                "status": str(row.status),
                "detail": "Бонус уже обработан",
            }

        # Идемпотентность
        if row.processing_idempotency_key:
            if str(row.processing_idempotency_key) == req.idempotency_key:
                logger.info(
                    "process_bonus_award: повтор с тем же idempotency_key=%s для bonus_awards.id=%s",
                    req.idempotency_key,
                    award_id,
                )
                return {
                    "ok": True,
                    "status": str(row.status),
                    "idempotency_key": req.idempotency_key,
                }
            else:
                raise ValueError("Бонус уже обрабатывается с другим idempotency_key")

        user_id = int(row.user_id)
        amount = d8(row.amount)
        src = str(row.source)

        meta = {
            "admin_id": admin.id,
            "admin_role": admin.role,
            "award_id": award_id,
            "source": src,
        }

        try:
            await credit_user_bonus_from_bank(
                db,
                user_id=user_id,
                amount=amount,
                reason=f"bonus_award:{src}",
                idempotency_key=req.idempotency_key,
                meta=meta,
            )
        except Exception as e:
            err_text = f"{type(e).__name__}: {e}"
            logger.warning(
                "process_bonus_award: credit_user_bonus_from_bank failed (award_id=%s, user_id=%s): %s",
                award_id,
                user_id,
                err_text,
            )
            await db.execute(
                text(
                    f"""
                    UPDATE {SCHEMA_ADMIN}.bonus_awards
                    SET status='ERROR',
                        processing_idempotency_key=:ik,
                        processed_at=NOW() AT TIME ZONE 'UTC'
                    WHERE id = :aid
                    """
                ),
                {"ik": req.idempotency_key, "aid": award_id},
            )
            await AdminLogger.write(
                db,
                admin_id=admin.id,
                action="BONUS_AWARD_ERROR",
                entity="bonus_awards",
                entity_id=award_id,
                details=err_text,
            )
            raise ValueError(f"Не удалось начислить бонус EFHC: {err_text}") from e

        await db.execute(
            text(
                f"""
                UPDATE {SCHEMA_ADMIN}.bonus_awards
                SET status='PAID',
                    processing_idempotency_key=:ik,
                    processed_at=NOW() AT TIME ZONE 'UTC'
                WHERE id = :aid
                """
            ),
            {"ik": req.idempotency_key, "aid": award_id},
        )

        await AdminLogger.write(
            db,
            admin_id=admin.id,
            action="BONUS_AWARD_PAID",
            entity="bonus_awards",
            entity_id=award_id,
            details=f"user_id={user_id}; amount={format_decimal_str(amount, EFHC_DECIMALS)}",
        )

        await AdminNotifier.notify_generic(
            db,
            event="BONUS_AWARD_PAID",
            message=(
                f"Выплачен бонус {format_decimal_str(amount, EFHC_DECIMALS)} "
                f"EFHC пользователю {user_id} (bonus)"
            ),
            payload_json=json.dumps(
                {
                    "award_id": award_id,
                    "user_id": user_id,
                    "amount": format_decimal_str(amount, EFHC_DECIMALS),
                    "source": src,
                    "admin_id": admin.id,
                },
                ensure_ascii=False,
            ),
        )

        return {
            "ok": True,
            "status": BonusAwardStatus.PAID,
            "idempotency_key": req.idempotency_key,
        }

    @staticmethod
    async def reject_bonus_award(
        db: AsyncSession,
        *,
        award_id: int,
        reason: str,
        admin: AdminUser,
    ) -> Dict[str, Any]:
        """
        Отклоняет бонусную выплату (без движения средств).

        Используется, если запись bonus_awards создана ошибочно или
        больше не актуальна.
        """
        RBAC.require_role(admin, AdminRole.MODERATOR)

        r: Result = await db.execute(
            text(
                f"""
                SELECT id, status
                FROM {SCHEMA_ADMIN}.bonus_awards
                WHERE id = :aid
                LIMIT 1
                """
            ),
            {"aid": award_id},
        )
        row = r.fetchone()
        if not row:
            raise ValueError("Бонусная запись не найдена")

        if row.status in (BonusAwardStatus.PAID, BonusAwardStatus.REJECTED):
            return {
                "ok": False,
                "status": str(row.status),
                "detail": "Бонус уже обработан",
            }

        await db.execute(
            text(
                f"""
                UPDATE {SCHEMA_ADMIN}.bonus_awards
                SET status='REJECTED',
                    processed_at=NOW() AT TIME ZONE 'UTC'
                WHERE id = :aid
                """
            ),
            {"aid": award_id},
        )

        await AdminLogger.write(
            db,
            admin_id=admin.id,
            action="BONUS_AWARD_REJECT",
            entity="bonus_awards",
            entity_id=award_id,
            details=reason,
        )

        await AdminNotifier.notify_generic(
            db,
            event="BONUS_AWARD_REJECT",
            message=f"Отклонён бонус #{award_id}: {reason}",
        )

        return {"ok": True, "status": BonusAwardStatus.REJECTED}

    # -------------------------------------------------------------------------
    # БЛОК 4. Заявки на призы (NFT и пр.) — prize_claims
    # -------------------------------------------------------------------------

    @staticmethod
    async def list_prize_claims(
        db: AsyncSession,
        filters: PrizeClaimFilters,
    ) -> List[PrizeClaimRecord]:
        """
        Возвращает список заявок на призы (NFT/прочее) из prize_claims.

        Особенности:
          • vip_only=True — фильтрация по пользователям с is_vip=TRUE.
          • prize_type — позволяет ограничить, например, только 'NFT_VIP'.
        """
        where = ["1=1"]
        params: Dict[str, Any] = {
            "limit": filters.limit,
            "offset": filters.offset,
        }
        if filters.user_id is not None:
            where.append("p.user_id = :uid")
            params["uid"] = filters.user_id
        if filters.status is not None:
            where.append("p.status = :st")
            params["st"] = filters.status
        if filters.prize_type is not None:
            where.append("p.prize_type = :ptype")
            params["ptype"] = filters.prize_type
        if filters.vip_only:
            where.append("u.is_vip = TRUE")

        order = "DESC" if filters.sort_desc else "ASC"

        sql = text(
            f"""
            SELECT
                p.id,
                p.lottery_id,
                p.user_id,
                p.prize_type,
                p.prize_value,
                p.wallet_address,
                p.status,
                p.created_at,
                p.processed_at,
                p.reject_reason,
                p.tx_hash,
                COALESCE(u.is_vip, FALSE) AS is_vip
            FROM {SCHEMA_ADMIN}.prize_claims p
            JOIN {SCHEMA_CORE}.users u ON u.id = p.user_id
            WHERE {" AND ".join(where)}
            ORDER BY p.id {order}
            LIMIT :limit OFFSET :offset
            """
        )
        r: Result = await db.execute(sql, params)
        out: List[PrizeClaimRecord] = []
        for row in r.fetchall():
            out.append(
                PrizeClaimRecord(
                    id=int(row.id),
                    lottery_id=int(row.lottery_id) if row.lottery_id is not None else None,
                    user_id=int(row.user_id),
                    prize_type=str(row.prize_type),
                    prize_value=str(row.prize_value),
                    wallet_address=row.wallet_address,
                    status=str(row.status),
                    created_at=row.created_at.isoformat()
                    if hasattr(row.created_at, "isoformat")
                    else str(row.created_at),
                    processed_at=row.processed_at.isoformat()
                    if getattr(row, "processed_at", None) and hasattr(row.processed_at, "isoformat")
                    else None,
                    reject_reason=row.reject_reason,
                    tx_hash=row.tx_hash,
                    is_vip=bool(row.is_vip),
                )
            )
        return out

    @staticmethod
    async def mark_prize_claim_done(
        db: AsyncSession,
        *,
        claim_id: int,
        req: PrizeClaimProcessRequest,
        admin: AdminUser,
    ) -> None:
        """
        Помечает заявку на приз как выполненную (например, NFT отправлен).

        Важно:
          • Не производит денежных операций (NFT/приз отдаются вне EFHC-банка).
          • Просто помечает статус 'DONE' и сохраняет tx_hash при наличии.
        """
        RBAC.require_role(admin, AdminRole.MODERATOR)

        r: Result = await db.execute(
            text(
                f"""
                SELECT id, status
                FROM {SCHEMA_ADMIN}.prize_claims
                WHERE id = :cid
                LIMIT 1
                """
            ),
            {"cid": claim_id},
        )
        row = r.fetchone()
        if not row:
            raise ValueError("Заявка на приз не найдена")

        if row.status in (PrizeClaimStatus.DONE, PrizeClaimStatus.REJECTED):
            return

        await db.execute(
            text(
                f"""
                UPDATE {SCHEMA_ADMIN}.prize_claims
                SET status='DONE',
                    processed_at=NOW() AT TIME ZONE 'UTC',
                    tx_hash=COALESCE(:txh, tx_hash)
                WHERE id = :cid
                """
            ),
            {"cid": claim_id, "txh": req.tx_hash},
        )

        await AdminLogger.write(
            db,
            admin_id=admin.id,
            action="PRIZE_DONE",
            entity="prize_claim",
            entity_id=claim_id,
            details=req.tx_hash or "",
        )

    @staticmethod
    async def reject_prize_claim(
        db: AsyncSession,
        *,
        claim_id: int,
        req: PrizeClaimRejectRequest,
        admin: AdminUser,
    ) -> None:
        """
        Отклоняет заявку на приз (например, если кошелёк неверен или данных недостаточно).

        Важно:
          • Денежных списаний/начислений здесь нет.
          • При необходимости возврата EFHC за покупку — это отдельная операция
            через BankService.manual_bank_user_transfer (Банк↔Пользователь).
        """
        RBAC.require_role(admin, AdminRole.MODERATOR)

        await db.execute(
            text(
                f"""
                UPDATE {SCHEMA_ADMIN}.prize_claims
                SET status='REJECTED',
                    processed_at=NOW() AT TIME ZONE 'UTC',
                    reject_reason=:reason
                WHERE id = :cid
                """
            ),
            {"cid": claim_id, "reason": req.reason[:500]},
        )

        await AdminLogger.write(
            db,
            admin_id=admin.id,
            action="PRIZE_REJECT",
            entity="prize_claim",
            entity_id=claim_id,
            details=req.reason,
        )


__all__ = [
    "WithdrawalStatus",
    "BonusAwardStatus",
    "PrizeClaimStatus",
    "WithdrawalFilters",
    "BonusAwardFilters",
    "PrizeClaimFilters",
    "WithdrawalRecord",
    "BonusAwardRecord",
    "PrizeClaimRecord",
    "WithdrawalApproveRequest",
    "BonusAwardProcessRequest",
    "PrizeClaimProcessRequest",
    "PrizeClaimRejectRequest",
    "WithdrawalsService",
]

