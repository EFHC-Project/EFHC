# -*- coding: utf-8 -*-
# backend/app/services/admin/admin_referral_service.py
# =============================================================================
# EFHC Bot — Реферальный сервис (бонусы, уровни, ИИ-идемпотентность)
# -----------------------------------------------------------------------------
# Назначение:
#   • Управление реферальными бонусами:
#       - прямой бонус за первую покупку панели рефералом;
#       - пороговые бонусы по количеству рефералов (10/100/1000/3000/10000 и др.).
#   • Все выплаты:
#       - только в БОНУСНЫЙ EFHC-баланс (канон);
#       - осуществляются ТОЛЬКО через банковский сервис
#         backend/app/services/transactions_service.py;
#       - строго идемпотентны (idempotency_key на уровне банка + уникальные
#         записи в ref_* таблицах).
#   • НИКАКИХ P2P-переводов между пользователями — только Банк ↔ Пользователь.
#
# Инварианты (канон):
#   1) Любое реферальное начисление идёт в бонусный баланс (bonus_balance).
#   2) Движение EFHC только через единый банковский сервис:
#        credit_user_bonus_from_bank(...)
#      Никаких прямых UPDATE user_balances/EFHC.
#   3) Идемпотентность:
#        • прямой бонус — один раз на invitee_id (ref_first_activation);
#        • пороговые бонусы — один раз на (referrer_id, threshold);
#        • на уровне банка всегда передаётся idempotency_key вида:
#              "ref:direct:<referrer_id>:<invitee_id>"
#              "ref:threshold:<referrer_id>:<threshold>"
#   4) В случае конкуренции (несколько воркеров/повторный вызов):
#        • ON CONFLICT в ref_* таблицах + idempotency_key в банке
#          гарантируют отсутствие «двойных» выплат.
#
# Для чайника:
#   • Этот модуль НЕ вызывает админские RBAC-проверки — он является
#     «сервисом домена рефералок» и вызывается из бизнес-событий (например,
#     при первой покупке панели) или из админ-панели.
#   • Деньги всегда начисляются через credit_user_bonus_from_bank(...),
#     который сам уменьшает баланс Банка EFHC и увеличивает бонусный баланс
#     пользователя, фиксируя запись в efhc_transfers_log.
# =============================================================================

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, List, Tuple

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
)
from backend.app.services.admin.admin_notifications import AdminNotifier

logger = get_logger(__name__)
S = get_settings()

SCHEMA_CORE: str = getattr(S, "DB_SCHEMA_CORE", "efhc_core") or "efhc_core"
SCHEMA_REF: str = getattr(S, "DB_SCHEMA_REFERRAL", "efhc_referral") or "efhc_referral"

# -----------------------------------------------------------------------------
# Реферальные настройки из конфига
# -----------------------------------------------------------------------------

# Бонус за первую покупку панели (по умолчанию 0.1 EFHC)
_REF_DIRECT_DEFAULT = "0.1"
REF_DIRECT_BONUS: Decimal = quantize_decimal(
    getattr(S, "REFERRAL_DIRECT_BONUS_EFHC", getattr(S, "REF_BONUS_ON_ACTIVATION_EFHC", _REF_DIRECT_DEFAULT)),
    8,
    "DOWN",
)

# Пороговые уровни (10:1,100:10,...) → list[(threshold, bonus)]
# Ожидается, что BaseSettings даёт parsed_ref_bonus_thresholds() с уже проверенным форматом.
REF_THRESHOLDS: List[Tuple[int, Decimal]] = [
    (int(k), quantize_decimal(v, 8, "DOWN"))
    for (k, v) in S.parsed_ref_bonus_thresholds()
]

# =============================================================================
# Специальные ошибки реферального сервиса
# =============================================================================

class ReferralError(Exception):
    """Базовая ошибка реферального сервиса."""

class ReferralConfigError(ReferralError):
    """Ошибка конфигурации (например, нулевой бонус/некорректные пороги)."""

class ReferralDataError(ReferralError):
    """Ошибка данных (несуществующий пользователь, отсутствие ссылок и т.п.)."""


# =============================================================================
# РЕФЕРАЛЬНЫЙ СЕРВИС
# =============================================================================

@dataclass
class DirectReferralResult:
    """Результат прямого бонуса за первую покупку панели."""
    paid: bool                  # True, если бонус был начислён, False — если уже платили
    referrer_id: int
    invitee_id: int
    amount_bonus: str           # строка EFHC (с 8 знаками)
    idempotency_key: str        # idempotency_key, использованный в банке


@dataclass
class ThresholdsReferralResult:
    """Результат выдачи пороговых бонусов."""
    referrer_id: int
    thresholds_paid: List[int]  # список достигнутых и впервые выплаченных порогов


class AdminReferralService:
    """
    Высокоуровневый сервис для работы с реферальными начислениями.

    Ключевые публичные методы:
      • award_direct_on_first_panel(...) — прямой бонус за первую панель.
      • award_threshold_bonuses(...)    — проверка и выдача пороговых бонусов.
    """

    # -------------------------------------------------------------------------
    # Прямой бонус за первую покупку панели (0.1 EFHC → бонусный счёт)
    # -------------------------------------------------------------------------

    @staticmethod
    async def award_direct_on_first_panel(
        db: AsyncSession,
        *,
        referrer_id: int,
        invitee_id: int,
    ) -> DirectReferralResult:
        """
        Начисляет ОДНОРАЗОВЫЙ бонус за первую покупку панели рефералом.

        Инварианты:
          • Выплата только один раз на invitee_id:
                ref_first_activation(invitee_id) имеет UNIQUE.
          • Сумма бонуса берётся из REF_DIRECT_BONUS и всегда идёт в бонусный
            EFHC-счёт через credit_user_bonus_from_bank(...).
          • Идемпотентность на уровне БАНКА:
                idempotency_key = f"ref:direct:{referrer_id}:{invitee_id}"

        Возвращает:
          • DirectReferralResult с флагом paid (True/False).

        Исключения:
          • ReferralConfigError — если бонус ≤ 0 (некорректная конфигурация).
          • SQLAlchemyError/DB-ошибки — пробрасываются наружу (пусть вызывающий код
            решает, откатывать ли транзакцию).
        """
        if REF_DIRECT_BONUS <= 0:
            raise ReferralConfigError("REF_DIRECT_BONUS_EFHC некорректен или равен 0 (канон требует > 0)")

        referrer_id = int(referrer_id)
        invitee_id = int(invitee_id)

        # 1) Фиксируем «первую активацию» в ref_first_activation с защитой от дублей.
        #    Если запись уже была — значит бонус уже начислялся (или будет начислен
        #    конкурентным воркером с тем же idempotency_key).
        r: Result = await db.execute(
            text(
                f"""
                INSERT INTO {SCHEMA_REF}.ref_first_activation (referrer_id, invitee_id, created_at)
                VALUES (:rid, :iid, NOW() AT TIME ZONE 'UTC')
                ON CONFLICT (invitee_id) DO NOTHING
                RETURNING invitee_id
                """
            ),
            {"rid": referrer_id, "iid": invitee_id},
        )
        inserted = r.fetchone()
        if not inserted:
            # Уже было событие первой активации: ничего не платим (идемпотентность).
            return DirectReferralResult(
                paid=False,
                referrer_id=referrer_id,
                invitee_id=invitee_id,
                amount_bonus=format_decimal_str(REF_DIRECT_BONUS, 8),
                idempotency_key=f"ref:direct:{referrer_id}:{invitee_id}",
            )

        # 2) Начисляем бонус на бонусный счёт реферера через БАНК.
        amount = quantize_decimal(REF_DIRECT_BONUS, 8, "DOWN")
        idem = f"ref:direct:{referrer_id}:{invitee_id}"

        try:
            await credit_user_bonus_from_bank(
                db,
                user_id=referrer_id,
                amount=amount,
                reason="ref_direct_bonus",
                idempotency_key=idem,
                meta={
                    "kind": "ref_direct",
                    "invitee_id": invitee_id,
                },
            )
        except Exception as e:
            # В случае ошибки начнётся откат транзакции выше по стеку.
            logger.error(
                "Referrals: ошибка начисления прямого бонуса (referrer=%s, invitee=%s): %s",
                referrer_id,
                invitee_id,
                e,
            )
            raise

        # 3) Уведомление админам (опционально)
        try:
            await AdminNotifier.notify_generic(
                db,
                event="REF_DIRECT_PAID",
                message=(
                    f"👥 Прямой реф-бонус\n"
                    f"Реферер: <code>{referrer_id}</code>\n"
                    f"Реферал: <code>{invitee_id}</code>\n"
                    f"Бонус: <b>{format_decimal_str(amount, 8)} EFHC (bonus)</b>"
                ),
                payload_json=(
                    f'{{"referrer_id":{referrer_id},'
                    f'"invitee_id":{invitee_id},'
                    f'"amount":"{format_decimal_str(amount, 8)}"}}'
                ),
                send_telegram=True,
            )
        except Exception as e:
            # Не роняем бизнес-логику из-за уведомлений
            logger.warning("Referrals: не удалось отправить REF_DIRECT_PAID уведомление: %s", e)

        return DirectReferralResult(
            paid=True,
            referrer_id=referrer_id,
            invitee_id=invitee_id,
            amount_bonus=format_decimal_str(amount, 8),
            idempotency_key=idem,
        )

    # -------------------------------------------------------------------------
    # Пороговые бонусы (10/100/1000/3000/10000 и др.) — только бонусный счёт
    # -------------------------------------------------------------------------

    @staticmethod
    async def award_threshold_bonuses(
        db: AsyncSession,
        *,
        referrer_id: int,
    ) -> ThresholdsReferralResult:
        """
        Проверяет, какие пороговые награды ещё не выплачивались рефереру,
        и выдаёт их на бонусный EFHC-счёт.

        Логика:
          1) Считаем общее число рефералов referrer_id по таблице ref_links.
          2) Смотрим, какие thresholds уже выплачены в ref_threshold_rewards.
          3) Для каждого (threshold, bonus) из REF_THRESHOLDS:
               - если total_refs >= threshold и threshold ещё НЕ выплачен:
                   • платим bonus на бонусный счёт через БАНК (idempotent);
                   • добавляем запись в ref_threshold_rewards;
                   • отправляем уведомление AdminNotifier.notify_ref_level(...).

        Идемпотентность:
          • ref_threshold_rewards имеет UNIQUE(referrer_id, threshold);
          • банковская операция использует idempotency_key:
                "ref:threshold:<referrer_id>:<threshold>"

        Возвращает:
          • ThresholdsReferralResult с перечислением порогов, по которым была
            произведена новая выплата в данном вызове.
        """
        referrer_id = int(referrer_id)

        # Если в конфиге вообще нет порогов — просто возвращаем пустой результат.
        if not REF_THRESHOLDS:
            return ThresholdsReferralResult(referrer_id=referrer_id, thresholds_paid=[])

        # 1) Общее количество рефералов
        r_total: Result = await db.execute(
            text(
                f"""
                SELECT COUNT(1) AS cnt
                FROM {SCHEMA_REF}.ref_links
                WHERE referrer_id = :uid
                """
            ),
            {"uid": referrer_id},
        )
        total_row = r_total.fetchone()
        total_refs = int(getattr(total_row, "cnt", 0) or 0)

        if total_refs <= 0:
            # Нет ни одного реферала — точно нечего выплачивать
            return ThresholdsReferralResult(referrer_id=referrer_id, thresholds_paid=[])

        # 2) Какие пороги уже были выплачены
        r_paid: Result = await db.execute(
            text(
                f"""
                SELECT threshold
                FROM {SCHEMA_REF}.ref_threshold_rewards
                WHERE referrer_id = :uid
                """
            ),
            {"uid": referrer_id},
        )
        paid_rows = r_paid.fetchall()
        already_paid: set[int] = {int(getattr(row, "threshold")) for row in paid_rows}

        newly_paid: List[int] = []

        # 3) Обход порогов в порядке возрастания (нормально, если REF_THRESHOLDS так задан)
        for threshold, bonus in REF_THRESHOLDS:
            thr = int(threshold)
            if total_refs < thr:
                # Порог ещё не достигнут
                continue
            if thr in already_paid:
                # За этот порог уже платили
                continue

            # Бонус должен быть > 0 (защита от некорректной конфигурации)
            bonus_q = quantize_decimal(bonus, 8, "DOWN")
            if bonus_q <= 0:
                logger.warning(
                    "Referrals: пороговый бонус <= 0 пропущен (referrer_id=%s, threshold=%s, bonus=%s)",
                    referrer_id,
                    thr,
                    bonus,
                )
                continue

            # 3.1) Начисляем бонус через БАНК
            idem = f"ref:threshold:{referrer_id}:{thr}"
            try:
                await credit_user_bonus_from_bank(
                    db,
                    user_id=referrer_id,
                    amount=bonus_q,
                    reason="ref_threshold_bonus",
                    idempotency_key=idem,
                    meta={
                        "kind": "ref_threshold",
                        "threshold": thr,
                        "total_refs": total_refs,
                    },
                )
            except Exception as e:
                logger.error(
                    "Referrals: ошибка начисления порогового бонуса (referrer=%s, threshold=%s): %s",
                    referrer_id,
                    thr,
                    e,
                )
                # Если банковская операция не прошла, не пишем запись о выплаченном
                # пороге — при следующем вызове сервис попробует ещё раз.
                continue

            # 3.2) Помечаем в ref_threshold_rewards, что порог выплачен (идемпотентность)
            try:
                await db.execute(
                    text(
                        f"""
                        INSERT INTO {SCHEMA_REF}.ref_threshold_rewards (referrer_id, threshold, paid_at)
                        VALUES (:uid, :thr, NOW() AT TIME ZONE 'UTC')
                        ON CONFLICT (referrer_id, threshold) DO NOTHING
                        """
                    ),
                    {"uid": referrer_id, "thr": thr},
                )
            except Exception as e:
                logger.error(
                    "Referrals: ошибка записи в ref_threshold_rewards (referrer=%s, threshold=%s): %s",
                    referrer_id,
                    thr,
                    e,
                )
                # Банковская операция уже прошла, но запись о пороге не была сделана.
                # Это не страшно с точки зрения денег (банк защищён idempotency_key),
                # но может привести к повторным попыткам начисления.
                # При следующем вызове банковская операция по тому же idem
                # должна быть идемпотентной (без повторного списания).
                # Поэтому порог всё равно считаем «достигнутым» в этом вызове:
                newly_paid.append(thr)
                continue

            newly_paid.append(thr)

            # 3.3) Уведомление админам о достижении порога
            try:
                await AdminNotifier.notify_ref_level(
                    db,
                    referrer_id=referrer_id,
                    threshold=thr,
                    bonus_efhc=format_decimal_str(bonus_q, 8),
                )
            except Exception as e:
                logger.warning(
                    "Referrals: не удалось отправить REFERRAL_LEVEL уведомление (referrer=%s, thr=%s): %s",
                    referrer_id,
                    thr,
                    e,
                )

        return ThresholdsReferralResult(referrer_id=referrer_id, thresholds_paid=newly_paid)


__all__ = [
    "ReferralError",
    "ReferralConfigError",
    "ReferralDataError",
    "DirectReferralResult",
    "ThresholdsReferralResult",
    "AdminReferralService",
]

