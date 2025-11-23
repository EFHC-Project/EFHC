# -*- coding: utf-8 -*-
# backend/app/services/admin/admin_users_service.py
# =============================================================================
# EFHC Bot — Пользователи / Прогресс / Ручные корректировки через Банк
# -----------------------------------------------------------------------------
# Назначение модуля:
#   • Безопасный просмотр состояния пользователя для админ-панели:
#       - балансы EFHC (основной/бонусный),
#       - энергия (available_kwh / total_generated_kwh),
#       - статус VIP,
#       - панели (активные/истёкшие, суммарная генерация по панелям).
#   • Открытие «окна прогресса» по Telegram ID — для отладки/исправлений.
#   • Ручные корректировки EFHC ТОЛЬКО через Банк:
#       - Банк → Пользователь (credit),
#       - Пользователь → Банк (debit),
#     с жёсткой идемпотентностью и логированием.
#
# Ключевые ИИ-инварианты:
#   1) Никаких P2P-переводов:
#        • В этом модуле и во всей админ-панели НЕТ переводов
#          Пользователь ↔ Пользователь.
#        • Допустимы только операции Банк ↔ Пользователь.
#   2) Все ручные корректировки EFHC проходят через
#        backend.app.services.transactions_service:
#          - credit_user_from_bank(...)
#          - credit_user_bonus_from_bank(...)
#          - debit_user_to_bank(...)
#          - debit_user_bonus_to_bank(...)
#      Этот сервис гарантирует:
#          • отсутствие «минуса» у пользователя,
#          • разрешённый минус у банка,
#          • идемпотентность по idempotency_key.
#   3) Все бонусные начисления идут только на бонусный счёт:
#        • balance_type = BONUS → bonus_balance,
#        • никакие бонусы не попадают на основной баланс.
#   4) Идемпотентность обязательна:
#        • idempotency_key передаётся снаружи (UI/роут),
#        • модуль НЕ генерирует ключи автоматически.
#   5) Коррекции энергии (kWh) в этом модуле не выполняются — только чтение.
#      Если потребуется отдельный механизм корректировки kWh, он будет
#      оформлен отдельным сервисом с собственной идемпотентностью.
# =============================================================================

from __future__ import annotations

from dataclasses import dataclass
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

from backend.app.services.transactions_service import (  # ЕДИНАЯ точка движения EFHC
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

SCHEMA_CORE: str = getattr(S, "DB_SCHEMA_CORE", "efhc_core") or "efhc_core"
SCHEMA_REF: str = getattr(S, "DB_SCHEMA_REFERRAL", "efhc_ref") or "efhc_ref"

EFHC_DECIMALS: int = int(getattr(S, "EFHC_DECIMALS", 8) or 8)
_Q = Decimal(1).scaleb(-EFHC_DECIMALS)


def d8(x: Any) -> Decimal:
    """Округление вниз до EFHC_DECIMALS знаков (канон)."""
    return Decimal(str(x)).quantize(_Q, rounding=ROUND_DOWN)


# =============================================================================
# DTO для админки (просмотр пользователя и прогресса)
# =============================================================================

class UserBalanceSnapshot(BaseModel):
    """Сводка по балансам и энергии пользователя."""
    user_id: int
    telegram_id: int
    is_vip: bool
    main_balance: str
    bonus_balance: str
    available_kwh: str
    total_generated_kwh: str
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class UserPanelsProgress(BaseModel):
    """Сводка по панелям пользователя."""
    total_panels: int
    active_panels: int
    expired_panels: int
    total_panel_generated_kwh: str


class UserReferralsBrief(BaseModel):
    """Короткая статистика по рефералам пользователя (для отладки)."""
    total_invited: int
    active_invited: int
    # При необходимости сюда можно добавить суммы бонусов и уровни


class UserProgressSnapshot(BaseModel):
    """
    Комплексная картинка прогресса пользователя:
      • балансы EFHC и энергия,
      • панели,
      • рефералы (кратко).
    """
    user: UserBalanceSnapshot
    panels: UserPanelsProgress
    referrals: UserReferralsBrief


# =============================================================================
# DTO для ручной корректировки EFHC (через Банк)
# =============================================================================

class AdjustUserBalanceRequest(BaseModel):
    """
    Ручная корректировка баланса пользователя через Банк.

    Направление:
      • direction = "IN"  — Банк → Пользователь  (credit*)
      • direction = "OUT" — Пользователь → Банк  (debit*)

    Тип баланса:
      • balance_type = "MAIN"  — основной счёт EFHC;
      • balance_type = "BONUS" — бонусный счёт EFHC (только для бонусов).

    Важно:
      • amount > 0
      • idempotency_key обязателен и задаётся админ-панелью.
      • НИКАКИХ переводов между пользователями тут нет.
    """
    user_id: int
    direction: Literal["IN", "OUT"]
    balance_type: Literal["MAIN", "BONUS"]
    amount: Any = Field(..., description="Сумма EFHC для корректировки")
    reason: str = Field(
        ...,
        min_length=1,
        max_length=120,
        description="Краткое описание причины (например, 'BUG_FIX_PANEL_DOUBLE_CHARGE')",
    )
    idempotency_key: str = Field(
        ...,
        min_length=8,
        max_length=128,
        description="Уникальный ключ идемпотентности операции (формируется на уровне UI/роута)",
    )

    @validator("amount", pre=True)
    def _v_amount(cls, v: Any) -> Decimal:
        dec = quantize_decimal(v, EFHC_DECIMALS, "DOWN")
        if dec <= 0:
            raise ValueError("Сумма должна быть больше 0")
        return dec


# =============================================================================
# Внутренние хелперы чтения пользователя
# =============================================================================

async def _get_user_row_by_telegram(db: AsyncSession, telegram_id: int) -> Optional[Any]:
    """
    Возвращает строку пользователя по telegram_id или None, если не найден.
    Таблица users (канон):
      id, telegram_id, is_vip, main_balance, bonus_balance,
      available_kwh, total_generated_kwh, created_at, updated_at.
    """
    r: Result = await db.execute(
        text(
            f"""
            SELECT
                id,
                telegram_id,
                COALESCE(is_vip, FALSE)            AS is_vip,
                COALESCE(main_balance, 0)          AS main_balance,
                COALESCE(bonus_balance, 0)         AS bonus_balance,
                COALESCE(available_kwh, 0)         AS available_kwh,
                COALESCE(total_generated_kwh, 0)   AS total_generated_kwh,
                created_at,
                updated_at
            FROM {SCHEMA_CORE}.users
            WHERE telegram_id = :tg
            LIMIT 1
            """
        ),
        {"tg": int(telegram_id)},
    )
    return r.fetchone()


async def _get_user_row_by_id(db: AsyncSession, user_id: int) -> Optional[Any]:
    """Аналогично _get_user_row_by_telegram, но по внутреннему id."""
    r: Result = await db.execute(
        text(
            f"""
            SELECT
                id,
                telegram_id,
                COALESCE(is_vip, FALSE)            AS is_vip,
                COALESCE(main_balance, 0)          AS main_balance,
                COALESCE(bonus_balance, 0)         AS bonus_balance,
                COALESCE(available_kwh, 0)         AS available_kwh,
                COALESCE(total_generated_kwh, 0)   AS total_generated_kwh,
                created_at,
                updated_at
            FROM {SCHEMA_CORE}.users
            WHERE id = :uid
            LIMIT 1
            """
        ),
        {"uid": int(user_id)},
    )
    return r.fetchone()


async def _get_panels_progress(db: AsyncSession, user_id: int) -> UserPanelsProgress:
    """
    Возвращает статистику панелей:
      • total_panels,
      • active_panels,
      • expired_panels,
      • суммарную генерацию по панелям (generated_kwh).
    """
    r: Result = await db.execute(
        text(
            f"""
            SELECT
                COUNT(1) AS total_panels,
                SUM(CASE WHEN is_active THEN 1 ELSE 0 END) AS active_panels,
                SUM(CASE WHEN is_active THEN 0 ELSE 1 END) AS expired_panels,
                COALESCE(SUM(COALESCE(generated_kwh, 0)), 0) AS total_gen
            FROM {SCHEMA_CORE}.panels
            WHERE user_id = :uid
            """
        ),
        {"uid": int(user_id)},
    )
    row = r.fetchone()
    if not row:
        return UserPanelsProgress(
            total_panels=0,
            active_panels=0,
            expired_panels=0,
            total_panel_generated_kwh="0",
        )
    total_panels = int(row.total_panels or 0)
    active_panels = int(row.active_panels or 0)
    expired_panels = int(row.expired_panels or 0)
    total_gen = d8(row.total_gen or 0)

    return UserPanelsProgress(
        total_panels=total_panels,
        active_panels=active_panels,
        expired_panels=expired_panels,
        total_panel_generated_kwh=format_decimal_str(total_gen, EFHC_DECIMALS),
    )


async def _get_referrals_brief(db: AsyncSession, user_id: int) -> UserReferralsBrief:
    """
    Возвращает короткую статистику по рефералам:
      • total_invited — всего приглашённых,
      • active_invited — активные рефералы (купили хотя бы одну панель).
    """
    # Всего рефералов (по ссылке)
    r1: Result = await db.execute(
        text(
            f"""
            SELECT COUNT(1) AS cnt
            FROM {SCHEMA_REF}.ref_links
            WHERE referrer_id = :uid
            """
        ),
        {"uid": int(user_id)},
    )
    row1 = r1.fetchone()
    total_invited = int(row1.cnt or 0) if row1 else 0

    # Активные рефералы — те, у кого есть хотя бы одна панель
    r2: Result = await db.execute(
        text(
            f"""
            SELECT COUNT(DISTINCT rl.invitee_id) AS cnt
            FROM {SCHEMA_REF}.ref_links rl
            JOIN {SCHEMA_CORE}.panels p
              ON p.user_id = rl.invitee_id
             AND p.is_active = TRUE
            WHERE rl.referrer_id = :uid
            """
        ),
        {"uid": int(user_id)},
    )
    row2 = r2.fetchone()
    active_invited = int(row2.cnt or 0) if row2 else 0

    return UserReferralsBrief(
        total_invited=total_invited,
        active_invited=active_invited,
    )


# =============================================================================
# Сервис AdminUsersService — просмотр/прогресс/корректировки через Банк
# =============================================================================

class AdminUsersService:
    """
    Сервис для админ-панели, отвечающий за:

      • поиск пользователей и просмотр сводки по балансам/энергии;
      • открытие «окна прогресса» по internal user_id или telegram_id;
      • ручные корректировки EFHC через Банк (идемпотентные кредит/дебет).

    Важно:
      • В этом модуле НЕТ P2P-переводов.
      • Все EFHC-операции проводятся через transactions_service.
      • Бонусы всегда идут на бонусный счёт.
    """

    # -------------------------------------------------------------------------
    # ЧТЕНИЕПОЛЬЗОВАТЕЛЕЙ / ПРОГРЕСС
    # -------------------------------------------------------------------------

    @staticmethod
    async def get_user_snapshot_by_id(db: AsyncSession, user_id: int) -> UserBalanceSnapshot:
        """Возвращает сводку по балансам и энергии пользователя по internal id."""
        row = await _get_user_row_by_id(db, user_id)
        if not row:
            raise ValueError("Пользователь не найден")

        return UserBalanceSnapshot(
            user_id=int(row.id),
            telegram_id=int(row.telegram_id),
            is_vip=bool(row.is_vip),
            main_balance=format_decimal_str(d8(row.main_balance), EFHC_DECIMALS),
            bonus_balance=format_decimal_str(d8(row.bonus_balance), EFHC_DECIMALS),
            available_kwh=format_decimal_str(d8(row.available_kwh), EFHC_DECIMALS),
            total_generated_kwh=format_decimal_str(d8(row.total_generated_kwh), EFHC_DECIMALS),
            created_at=row.created_at.isoformat() if getattr(row, "created_at", None) and hasattr(row.created_at, "isoformat") else None,
            updated_at=row.updated_at.isoformat() if getattr(row, "updated_at", None) and hasattr(row.updated_at, "isoformat") else None,
        )

    @staticmethod
    async def get_user_snapshot_by_telegram(db: AsyncSession, telegram_id: int) -> UserBalanceSnapshot:
        """Возвращает сводку по балансам и энергии пользователя по Telegram ID."""
        row = await _get_user_row_by_telegram(db, telegram_id)
        if not row:
            raise ValueError("Пользователь не найден")

        return UserBalanceSnapshot(
            user_id=int(row.id),
            telegram_id=int(row.telegram_id),
            is_vip=bool(row.is_vip),
            main_balance=format_decimal_str(d8(row.main_balance), EFHC_DECIMALS),
            bonus_balance=format_decimal_str(d8(row.bonus_balance), EFHC_DECIMALS),
            available_kwh=format_decimal_str(d8(row.available_kwh), EFHC_DECIMALS),
            total_generated_kwh=format_decimal_str(d8(row.total_generated_kwh), EFHC_DECIMALS),
            created_at=row.created_at.isoformat() if getattr(row, "created_at", None) and hasattr(row.created_at, "isoformat") else None,
            updated_at=row.updated_at.isoformat() if getattr(row, "updated_at", None) and hasattr(row.updated_at, "isoformat") else None,
        )

    @staticmethod
    async def get_user_progress_by_id(db: AsyncSession, user_id: int) -> UserProgressSnapshot:
        """
        Возвращает полную картинку прогресса пользователя по internal id:
          • балансы/энергия,
          • панели,
          • рефералка.
        """
        user_snap = await AdminUsersService.get_user_snapshot_by_id(db, user_id)
        panels = await _get_panels_progress(db, user_id=user_snap.user_id)
        refs = await _get_referrals_brief(db, user_id=user_snap.user_id)
        return UserProgressSnapshot(user=user_snap, panels=panels, referrals=refs)

    @staticmethod
    async def get_user_progress_by_telegram(db: AsyncSession, telegram_id: int) -> UserProgressSnapshot:
        """
        Специальная функция для «открытия окна игры» по Telegram ID.
        Удобно для админ-панели: поиск по Telegram → просмотр прогресса.
        """
        row = await _get_user_row_by_telegram(db, telegram_id)
        if not row:
            raise ValueError("Пользователь не найден")
        user_id = int(row.id)

        user_snap = await AdminUsersService.get_user_snapshot_by_id(db, user_id)
        panels = await _get_panels_progress(db, user_id=user_id)
        refs = await _get_referrals_brief(db, user_id=user_id)
        return UserProgressSnapshot(user=user_snap, panels=panels, referrals=refs)

    @staticmethod
    async def search_users_by_telegram_prefix(
        db: AsyncSession,
        *,
        telegram_prefix: str,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """
        Утилита для поиска пользователей по началу Telegram ID (как строка)
        или точному совпадению. Используется в админ-поиске.

        Важно:
          • Возвращает только минимальный набор полей — безопасно.
        """
        limit = max(1, min(limit, 100))
        prefix = telegram_prefix.strip()
        if not prefix:
            raise ValueError("Пустой префикс Telegram ID")

        # Простая реализация: если строка целиком число — можно искать по =,
        # иначе — по LIKE. Логику можно усложнить при необходимости.
        where = []
        params: Dict[str, Any] = {"limit": limit}

        if prefix.isdigit():
            where.append("CAST(telegram_id AS TEXT) LIKE :pref")
            params["pref"] = f"{prefix}%"
        else:
            where.append("CAST(telegram_id AS TEXT) LIKE :pref")
            params["pref"] = f"{prefix}%"

        sql = text(
            f"""
            SELECT
                id,
                telegram_id,
                COALESCE(is_vip, FALSE) AS is_vip,
                COALESCE(main_balance, 0) AS main_balance,
                COALESCE(bonus_balance, 0) AS bonus_balance
            FROM {SCHEMA_CORE}.users
            WHERE {" AND ".join(where)}
            ORDER BY id DESC
            LIMIT :limit
            """
        )
        r: Result = await db.execute(sql, params)
        out: List[Dict[str, Any]] = []
        for row in r.fetchall():
            out.append(
                {
                    "user_id": int(row.id),
                    "telegram_id": int(row.telegram_id),
                    "is_vip": bool(row.is_vip),
                    "main_balance": format_decimal_str(d8(row.main_balance), EFHC_DECIMALS),
                    "bonus_balance": format_decimal_str(d8(row.bonus_balance), EFHC_DECIMALS),
                }
            )
        return out

    # -------------------------------------------------------------------------
    # РУЧНЫЕ КОРРЕКТИРОВКИ EFHC ЧЕРЕЗ БАНК (ИДЕМПОТЕНТНО)
    # -------------------------------------------------------------------------

    @staticmethod
    async def adjust_user_balance_via_bank(
        db: AsyncSession,
        req: AdjustUserBalanceRequest,
        admin: AdminUser,
    ) -> Dict[str, Any]:
        """
        Выполняет ручную корректировку EFHC через Банк.

        Канон:
          • direction = "IN"  → Банк → Пользователь (пополнение)
          • direction = "OUT" → Пользователь → Банк (списание)
          • balance_type = "MAIN" / "BONUS"
          • НИКАКИХ P2P-переводов.
          • Все операции идемпотентны по idempotency_key.

        Реализация:
          • вызывает один из методов transactions_service:
              - credit_user_from_bank(...)
              - credit_user_bonus_from_bank(...)
              - debit_user_to_bank(...)
              - debit_user_bonus_to_bank(...)
          • на уровне transactions_service:
              - защита от отрицательных балансов у пользователя,
              - разрешённый минус у банка,
              - логирование в банк/журнал трансферов.
        """
        RBAC.require_role(admin, AdminRole.MODERATOR)

        # Проверяем, что пользователь существует (для дружелюбной ошибки)
        row = await _get_user_row_by_id(db, req.user_id)
        if not row:
            raise ValueError("Пользователь не найден")

        amount: Decimal = req.amount  # уже квантизирован в валидаторе
        idem = req.idempotency_key.strip()

        if not idem:
            # Жёсткая защита: ключ идемпотентности обязателен
            raise ValueError("Пустой idempotency_key запрещён")

        reason = f"ADMIN_{req.reason.strip().upper()[:100]}"

        # Выбор операции по направлению и типу баланса
        try:
            if req.direction == "IN":
                if req.balance_type == "MAIN":
                    balances = await credit_user_from_bank(
                        db,
                        user_id=req.user_id,
                        amount=amount,
                        idempotency_key=idem,
                        reason=reason,
                    )
                else:  # BONUS
                    balances = await credit_user_bonus_from_bank(
                        db,
                        user_id=req.user_id,
                        amount=amount,
                        idempotency_key=idem,
                        reason=reason,
                    )
            else:  # direction == "OUT"
                if req.balance_type == "MAIN":
                    balances = await debit_user_to_bank(
                        db,
                        user_id=req.user_id,
                        amount=amount,
                        idempotency_key=idem,
                        reason=reason,
                    )
                else:  # BONUS
                    balances = await debit_user_bonus_to_bank(
                        db,
                        user_id=req.user_id,
                        amount=amount,
                        idempotency_key=idem,
                        reason=reason,
                    )

        except Exception as e:  # noqa: BLE001
            # Логируем как системную ошибку, чтобы отразить в админке
            logger.warning(
                "adjust_user_balance_via_bank: failed for user=%s, direction=%s, type=%s, amount=%s, idem=%s: %s",
                req.user_id,
                req.direction,
                req.balance_type,
                amount,
                idem,
                e,
            )
            # Пробрасываем дальше — роут/слой выше сформирует ответ UI
            raise

        # Лог действий админа
        await AdminLogger.write(
            db,
            admin_id=admin.id,
            action="USER_BALANCE_ADJUST",
            entity="user_balance",
            entity_id=req.user_id,
            details=f"direction={req.direction}; type={req.balance_type}; amount={format_decimal_str(amount, EFHC_DECIMALS)}; idem={idem}",
        )

        # Уведомление (по желанию — без критической важности)
        await AdminNotifier.notify_generic(
            db,
            event="USER_BALANCE_ADJUST",
            message=(
                f"Ручная корректировка баланса пользователя {req.user_id}: "
                f"{req.direction} {format_decimal_str(amount, EFHC_DECIMALS)} EFHC ({req.balance_type})"
            ),
            payload_json=(
                f'{{"user_id":{req.user_id},"direction":"{req.direction}",'
                f'"balance_type":"{req.balance_type}","amount":"{format_decimal_str(amount, EFHC_DECIMALS)}"}}'
            ),
        )

        # balances — объект/словарь, возвращаемый transactions_service;
        # предполагаем, что в нём есть поля main_balance/bonus_balance/available_kwh/total_generated_kwh.
        return {
            "ok": True,
            "user_id": req.user_id,
            "direction": req.direction,
            "balance_type": req.balance_type,
            "amount": format_decimal_str(amount, EFHC_DECIMALS),
            "idempotency_key": idem,
            "new_main_balance": format_decimal_str(d8(balances.get("main_balance", 0)), EFHC_DECIMALS),
            "new_bonus_balance": format_decimal_str(d8(balances.get("bonus_balance", 0)), EFHC_DECIMALS),
        }


__all__ = [
    "UserBalanceSnapshot",
    "UserPanelsProgress",
    "UserReferralsBrief",
    "UserProgressSnapshot",
    "AdjustUserBalanceRequest",
    "AdminUsersService",
]

