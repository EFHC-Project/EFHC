# -*- coding: utf-8 -*-
# backend/app/services/wallet_binding_service.py
# =============================================================================
# Назначение:
#   • Управление привязкой TON-кошелька к пользователю EFHC Bot:
#       - старт привязки (генерация nonce и инструкции),
#       - подтверждение по on-chain транзакции с MEMO BIND:{tid}:{nonce},
#       - перепривязка (старый → неактивный, новый → активный),
#       - блокировка/разблокировка кошелька админом,
#       - (опционально) мульти-привязка с ограничением по количеству.
#
# Главное правило проекта:
#   • По умолчанию у пользователя ДОЛЖЕН быть ровно ОДИН активный кошелёк.
#   • Любые депозиты EFHC без memo допускаются ТОЛЬКО с привязанного активного
#     кошелька (см. efhc_deposit_service.process_efhc_deposit_tx).
#
# Как подтверждаем владение адресом (без «заглушек» и внешних SDK):
#   • Через маленький on-chain перевод на адрес проекта с MEMO:
#       BIND:{telegram_id}:{nonce}
#     — его отслеживает наш TON watcher и вызывает finalize_bind_by_tx(...)
#     — адрес отправителя = кандидат на привязку; транзакция = доказательство.
#
# Где используется:
#   • user_routes (профиль): показать текущий активный кошелёк/статус.
#   • frontend/WebApp: при нажатии «Привязать кошелёк» получаем nonce+memo+инструкцию.
#   • watcher (TON): при появлении BIND-транзакции вызывает finalize_bind_by_tx.
#
# Требования к БД (миграции Alembic):
#   1) efhc_core.user_wallets:
#        - id             bigserial PK
#        - user_id        bigint (Telegram ID), indexed
#        - ton_address    text UNIQUE NOT NULL
#        - is_active      boolean NOT NULL DEFAULT true
#        - is_blocked     boolean NOT NULL DEFAULT false
#        - is_primary     boolean NOT NULL DEFAULT true  (для будущей мульти-привязки)
#        - created_at     timestamptz NOT NULL DEFAULT now()
#        - updated_at     timestamptz NOT NULL DEFAULT now()
#        Индексы/ограничения:
#          * UNIQUE(ton_address)
#          * PARTIAL UNIQUE (user_id WHERE is_active=true)  -- обеспечивает "только 1 активный"
#
#   2) efhc_core.wallet_bind_requests:
#        - id             bigserial PK
#        - user_id        bigint NOT NULL
#        - candidate_addr text NOT NULL
#        - nonce          text NOT NULL
#        - status         text NOT NULL  ('pending'|'confirmed'|'expired'|'canceled')
#        - expires_at     timestamptz NULL
#        - tx_hash        text NULL
#        - created_at     timestamptz NOT NULL DEFAULT now()
#        - updated_at     timestamptz NOT NULL DEFAULT now()
#        Индексы:
#          * UNIQUE(nonce)     -- один челлендж = одна заявка
#          * (user_id, status) -- быстрый поиск активных запросов
#
# ИИ-защита / самовосстановление:
#   • start_bind_request:
#       - проверяет конфликт адреса (владелец/блокировка);
#       - уважает лимит активных кошельков;
#       - автоматически снимает прошлые pending-заявки пользователя.
#   • finalize_bind_by_tx:
#       - идемпотентен по (user_id, nonce, tx_hash):
#           • повторный вызов с тем же tx_hash → безопасный replay "ok_replay",
#           • если заявка уже confirmed/expired/canceled → мягкий skip.
#       - использует row-level блокировку заявки (FOR UPDATE) для защиты от гонок.
#       - атомарно переключает активный кошелёк (старый → inactive, новый → active).
#   • Блокировка/разблокировка кошелька никогда не ломает привязку других пользователей.
# =============================================================================

from __future__ import annotations

import secrets
import string
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config_core import get_settings
from backend.app.core.logging_core import get_logger

# -----------------------------------------------------------------------------
# Настройки и логгер
# -----------------------------------------------------------------------------
settings = get_settings()
logger = get_logger(__name__)

CORE_SCHEMA: str = getattr(settings, "DB_SCHEMA_CORE", "efhc_core")
# Каноническое название кошелька проекта в настройках — TON_MAIN_WALLET
MAIN_TON_WALLET: str = getattr(settings, "TON_MAIN_WALLET", "") or ""

if not MAIN_TON_WALLET:
    logger.warning("[WALLET_BIND] TON_MAIN_WALLET не задан в конфигурации. BIND-транзакции работать не будут.")

# Управление политикой привязок:
MAX_WALLETS_PER_USER: int = int(getattr(settings, "MAX_WALLETS_PER_USER", 1) or 1)
BIND_REQUEST_TTL_MIN: int = int(getattr(settings, "BIND_REQUEST_TTL_MIN", 15) or 15)

# Формат челленджа в MEMO on-chain перевода:
#   BIND:{telegram_id}:{nonce}
BIND_MEMO_PREFIX = "BIND"


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


# -----------------------------------------------------------------------------
# Ошибки предметной области (понятные вызывающему коду)
# -----------------------------------------------------------------------------
class WalletError(Exception):
    """Базовая ошибка привязки кошелька."""


class AddressInUse(WalletError):
    """Адрес уже привязан к другому пользователю или заблокирован."""


class ActiveWalletLimit(WalletError):
    """Превышен лимит активных кошельков для пользователя."""


class NoPendingRequest(WalletError):
    """Нет ожидающей заявки на привязку (nonce не найден/просрочен/закрыт)."""


class AddressMismatch(WalletError):
    """Отправитель транзакции не совпадает с candidate_addr заявки."""


class AlreadyBound(WalletError):
    """У пользователя уже активен этот адрес."""


# -----------------------------------------------------------------------------
# DTO для ответов сервиса (удобно возвращать наружу)
# -----------------------------------------------------------------------------
@dataclass(slots=True)
class BindStartResult:
    user_id: int
    candidate_addr: str
    nonce: str
    memo: str
    to_wallet: str
    expires_at: datetime


@dataclass(slots=True)
class WalletState:
    user_id: int
    active_address: Optional[str]
    is_blocked: bool
    bind_pending: bool
    bind_nonce: Optional[str]


# =============================================================================
# Публичный API
# =============================================================================
async def start_bind_request(
    db: AsyncSession,
    *,
    user_id: int,
    candidate_addr: str,
) -> BindStartResult:
    """
    Старт привязки: создаёт заявку с nonce и возвращает инструкцию для on-chain подтверждения.

    Что происходит «для чайника»:
      • Генерируем одноразовый код (nonce), который надо передать в MEMO on-chain перевода.
      • Пользователь делает маленький перевод на адрес проекта (MAIN_TON_WALLET)
        с MEMO 'BIND:{user_id}:{nonce}'. Эту транзакцию увидит watcher.
      • В watcher при получении транзакции вызываем finalize_bind_by_tx(...), который
        проверит совпадение адреса-отправителя и nonce, и активирует кошелёк.

    ИИ-защита:
      • Адрес не может принадлежать другому пользователю или быть заблокированным.
      • Если адрес уже активен у пользователя — выбрасываем AlreadyBound.
      • Учитываем лимит активных кошельков. При превышении — ActiveWalletLimit.
      • Старые pending-заявки пользователя переводим в 'canceled', чтобы не плодить мусор.
    """
    candidate_addr = _norm_addr(candidate_addr)

    # 0) Базовые проверки конфигурации
    if not MAIN_TON_WALLET:
        raise WalletError("TON_MAIN_WALLET не настроен на сервере, привязка невозможна.")

    # 1) Запретить присвоение адреса, если он занят другим пользователем/заблокирован
    owner = await _find_wallet_owner(db, candidate_addr)
    if owner and owner != user_id:
        raise AddressInUse("Этот адрес уже привязан к другому пользователю.")
    if await _is_wallet_blocked(db, candidate_addr):
        raise AddressInUse("Этот адрес заблокирован и не может быть привязан.")

    # 2) Если у пользователя уже активен именно этот адрес — нет смысла начинать
    active_addr = await _get_active_wallet(db, user_id)
    if active_addr and _norm_addr(active_addr) == candidate_addr:
        raise AlreadyBound("Этот кошелёк уже активен у пользователя.")

    # 3) Проверяем лимит активных кошельков (по умолчанию 1)
    active_count = await _count_active_wallets(db, user_id)
    if active_count >= MAX_WALLETS_PER_USER:
        raise ActiveWalletLimit(
            f"Превышен лимит активных кошельков ({MAX_WALLETS_PER_USER}). "
            "Сначала отключите текущий, затем привяжите новый."
        )

    # 4) Снимаем старые pending-заявки пользователя (мягкая ИИ-очистка мусора)
    await db.execute(
        text(
            f"""
            UPDATE {CORE_SCHEMA}.wallet_bind_requests
               SET status = 'canceled', updated_at = now()
             WHERE user_id = :uid AND status = 'pending'
            """
        ),
        {"uid": user_id},
    )

    # 5) Создаём новую заявку с nonce и временем жизни
    nonce = _gen_nonce()
    expires_at = _now_utc() + timedelta(minutes=BIND_REQUEST_TTL_MIN)

    await db.execute(
        text(
            f"""
            INSERT INTO {CORE_SCHEMA}.wallet_bind_requests
                (user_id, candidate_addr, nonce, status, expires_at)
            VALUES (:uid, :addr, :nonce, 'pending', :exp)
            """
        ),
        {"uid": user_id, "addr": candidate_addr, "nonce": nonce, "exp": expires_at},
    )
    await db.commit()

    memo = f"{BIND_MEMO_PREFIX}:{user_id}:{nonce}"

    return BindStartResult(
        user_id=user_id,
        candidate_addr=candidate_addr,
        nonce=nonce,
        memo=memo,
        to_wallet=MAIN_TON_WALLET,
        expires_at=expires_at,
    )


async def finalize_bind_by_tx(
    db: AsyncSession,
    *,
    user_id: int,
    from_address: str,
    memo: str,
    tx_hash: str,
) -> str:
    """
    Финализация привязки — вызывается из watcher, когда пришла on-chain транзакция:
      • to == MAIN_TON_WALLET
      • memo == 'BIND:{user_id}:{nonce}'
      • from == кандидат на привязку (доказательство владения)

    Действия:
      1) Парсим MEMO, убеждаемся, что он относится к этому user_id.
      2) Находим заявку по nonce (последнюю), блокируем её FOR UPDATE.
      3) Обрабатываем статусы:
           - pending  → продолжаем финализацию;
           - confirmed с тем же tx_hash → "ok_replay" (идемпотентный повтор);
           - confirmed с другим tx_hash / expired / canceled → мягкий skip.
      4) Проверяем, что from_address совпадает с candidate_addr.
      5) Если MAX_WALLETS_PER_USER == 1, отключаем прошлый активный кошелёк.
      6) Активируем/создаём запись в user_wallets.
      7) Помечаем заявку как 'confirmed', сохраняем tx_hash.

    ИИ-защита:
      • Полностью идемпотентная обработка по связке (user_id, nonce, tx_hash).
      • Гонки watcher-а решаются через SELECT ... FOR UPDATE.
      • Любые повторы обработанного tx_hash НЕ ломают состояние (ok_replay).
    """
    if not MAIN_TON_WALLET:
        logger.warning("[WALLET_BIND] finalize_bind_by_tx вызван без настроенного TON_MAIN_WALLET")
        return "error:config"

    parsed = _parse_bind_memo(memo)
    if not parsed or parsed[0] != user_id:
        # не наш memo — мягкий skip
        return "skip:not_our_memo"

    _, nonce = parsed
    from_address = _norm_addr(from_address)

    try:
        await db.begin()

        # 1) Находим заявку и блокируем её
        req = await _get_request_for_update(db, user_id, nonce)
        if not req:
            # заявки нет вообще → либо протухла, либо удалена
            await db.rollback()
            raise NoPendingRequest("Нет заявки с таким nonce.")

        req_id = req["id"]
        candidate_addr = _norm_addr(req["candidate_addr"])
        status = req["status"]
        expires_at = req["expires_at"]
        prev_tx_hash = req["tx_hash"]

        # 2) Проверяем TTL
        if expires_at and expires_at < _now_utc():
            # протухла → помечаем expired (если ещё не помечена)
            if status == "pending":
                await db.execute(
                    text(
                        f"""
                        UPDATE {CORE_SCHEMA}.wallet_bind_requests
                           SET status = 'expired', updated_at = now()
                         WHERE id = :rid AND status = 'pending'
                        """
                    ),
                    {"rid": req_id},
                )
            await db.commit()
            raise NoPendingRequest("Заявка на привязку истекла.")

        # 3) Идемпотентность по статусу заявки
        if status == "confirmed":
            # если тот же tx_hash → безопасный replay
            if prev_tx_hash and prev_tx_hash == tx_hash:
                await db.commit()
                return "ok_replay"
            await db.commit()
            return "skip:already_confirmed"
        if status in ("expired", "canceled"):
            await db.commit()
            raise NoPendingRequest(f"Заявка уже имеет статус '{status}'.")

        # 4) Статус pending → проверяем совпадение адресов
        if candidate_addr != from_address:
            await db.rollback()
            raise AddressMismatch("Адрес отправителя не совпадает с заявкой.")

        # 5) Если лимит активных кошельков = 1, выключаем предыдущий активный
        if MAX_WALLETS_PER_USER == 1:
            await db.execute(
                text(
                    f"""
                    UPDATE {CORE_SCHEMA}.user_wallets
                       SET is_active = false, is_primary = false, updated_at = now()
                     WHERE user_id = :uid AND is_active IS TRUE
                    """
                ),
                {"uid": user_id},
            )

        # 6) Активируем/создаём запись в user_wallets
        existing = await db.execute(
            text(
                f"""
                SELECT id, is_blocked
                  FROM {CORE_SCHEMA}.user_wallets
                 WHERE ton_address = :addr
                 LIMIT 1
                """
            ),
            {"addr": from_address},
        )
        row = existing.fetchone()
        if row:
            if bool(row[1]):
                await db.rollback()
                raise AddressInUse("Этот адрес заблокирован и не может быть активирован.")
            await db.execute(
                text(
                    f"""
                    UPDATE {CORE_SCHEMA}.user_wallets
                       SET user_id = :uid,
                           is_active = true,
                           is_primary = true,
                           updated_at = now()
                     WHERE ton_address = :addr
                    """
                ),
                {"uid": user_id, "addr": from_address},
            )
        else:
            await db.execute(
                text(
                    f"""
                    INSERT INTO {CORE_SCHEMA}.user_wallets
                        (user_id, ton_address, is_active, is_blocked, is_primary)
                    VALUES (:uid, :addr, true, false, true)
                    """
                ),
                {"uid": user_id, "addr": from_address},
            )

        # 7) Закрываем заявку
        await db.execute(
            text(
                f"""
                UPDATE {CORE_SCHEMA}.wallet_bind_requests
                   SET status = 'confirmed', tx_hash = :tx, updated_at = now()
                 WHERE id = :rid AND status = 'pending'
                """
            ),
            {"rid": req_id, "tx": tx_hash},
        )

        await db.commit()
        logger.info("[WALLET_BIND] user=%s bound wallet=%s via tx=%s", user_id, from_address, tx_hash)
        return "ok"
    except Exception:
        try:
            await db.rollback()
        except Exception:
            pass
        raise


async def block_active_wallet(
    db: AsyncSession,
    *,
    user_id: int,
    reason: str = "",
) -> Optional[str]:
    """
    Блокирует текущий активный кошелёк пользователя (is_blocked=true, is_active=false).
    Возвращает адрес, который заблокирован, либо None, если активного не было.

    Для чайника:
      • Заблокированный кошелёк НЕ сможет зачислить депозит (watcher его проигнорирует).
      • Разблокировать можно функцией unblock_wallet(addr) (ниже).
    """
    row = await db.execute(
        text(
            f"""
            SELECT ton_address
              FROM {CORE_SCHEMA}.user_wallets
             WHERE user_id = :uid AND is_active IS TRUE
             LIMIT 1
            """
        ),
        {"uid": user_id},
    )
    r = row.fetchone()
    if not r:
        return None
    addr = _norm_addr(r[0])

    await db.execute(
        text(
            f"""
            UPDATE {CORE_SCHEMA}.user_wallets
               SET is_active = false, is_blocked = true, is_primary = false, updated_at = now()
             WHERE user_id = :uid AND ton_address = :addr
            """
        ),
        {"uid": user_id, "addr": addr},
    )
    await db.commit()
    logger.info("[WALLET_BIND] blocked addr=%s for user=%s; reason=%s", addr, user_id, reason)
    # (при необходимости можно дополнительно писать в отдельный лог-таблицу причин блокировок)
    return addr


async def unblock_wallet(
    db: AsyncSession,
    *,
    address: str,
) -> bool:
    """
    Разблокировка кошелька (is_blocked=false). Активным НЕ становится автоматически.
    Возвращает True, если статус изменён.
    """
    addr = _norm_addr(address)
    res = await db.execute(
        text(
            f"""
            UPDATE {CORE_SCHEMA}.user_wallets
               SET is_blocked = false, updated_at = now()
             WHERE ton_address = :addr AND is_blocked IS TRUE
            """
        ),
        {"addr": addr},
    )
    await db.commit()
    return res.rowcount > 0


async def get_wallet_state(db: AsyncSession, *, user_id: int) -> WalletState:
    """
    Возвращает текущее состояние кошелька пользователя:
      • active_address — активный адрес (или None),
      • is_blocked     — если активный есть, заблокирован ли он,
      • bind_pending   — есть ли незавершённая заявка,
      • bind_nonce     — nonce последней 'pending' заявки (если есть).
    """
    active = await db.execute(
        text(
            f"""
            SELECT ton_address, is_blocked
              FROM {CORE_SCHEMA}.user_wallets
             WHERE user_id = :uid AND is_active IS TRUE
             LIMIT 1
            """
        ),
        {"uid": user_id},
    )
    r = active.fetchone()
    active_addr = _norm_addr(r[0]) if r else None
    is_blocked = bool(r[1]) if r else False

    pending = await db.execute(
        text(
            f"""
            SELECT nonce
              FROM {CORE_SCHEMA}.wallet_bind_requests
             WHERE user_id = :uid AND status = 'pending'
             ORDER BY created_at DESC
             LIMIT 1
            """
        ),
        {"uid": user_id},
    )
    p = pending.fetchone()
    return WalletState(
        user_id=user_id,
        active_address=active_addr,
        is_blocked=is_blocked,
        bind_pending=bool(p),
        bind_nonce=str(p[0]) if p else None,
    )


# =============================================================================
# Вспомогательные функции
# =============================================================================
def _norm_addr(addr: str) -> str:
    """Простейшая нормализация TON-адреса: убираем пробелы."""
    return (addr or "").replace(" ", "")


def _gen_nonce(length: int = 12) -> str:
    """Генерация безопасного одноразового кода для MEMO."""
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _parse_bind_memo(memo: str) -> Optional[Tuple[int, str]]:
    """
    Разбирает MEMO формата 'BIND:{user_id}:{nonce}' → (user_id, nonce) или None.
    """
    if not memo:
        return None
    parts = memo.strip().split(":")
    if len(parts) != 3:
        return None
    if parts[0].upper() != BIND_MEMO_PREFIX:
        return None
    try:
        uid = int(parts[1])
    except ValueError:
        return None
    nonce = parts[2]
    if not nonce or len(nonce) > 64:
        return None
    return (uid, nonce)


async def _find_wallet_owner(db: AsyncSession, address: str) -> Optional[int]:
    """
    Возвращает user_id владельца адреса из user_wallets (если есть).
    """
    addr = _norm_addr(address)
    row = await db.execute(
        text(
            f"""
            SELECT user_id
              FROM {CORE_SCHEMA}.user_wallets
             WHERE ton_address = :addr
             LIMIT 1
            """
        ),
        {"addr": addr},
    )
    r = row.fetchone()
    return int(r[0]) if r and r[0] is not None else None


async def _is_wallet_blocked(db: AsyncSession, address: str) -> bool:
    """
    True, если адрес помечен заблокированным (is_blocked=true) в user_wallets.
    """
    addr = _norm_addr(address)
    row = await db.execute(
        text(
            f"""
            SELECT is_blocked
              FROM {CORE_SCHEMA}.user_wallets
             WHERE ton_address = :addr
             LIMIT 1
            """
        ),
        {"addr": addr},
    )
    r = row.fetchone()
    return bool(r[0]) if r else False


async def _get_active_wallet(db: AsyncSession, user_id: int) -> Optional[str]:
    """
    Возвращает активный адрес пользователя (или None).
    """
    row = await db.execute(
        text(
            f"""
            SELECT ton_address
              FROM {CORE_SCHEMA}.user_wallets
             WHERE user_id = :uid AND is_active IS TRUE
             LIMIT 1
            """
        ),
        {"uid": user_id},
    )
    r = row.fetchone()
    return _norm_addr(r[0]) if r else None


async def _count_active_wallets(db: AsyncSession, user_id: int) -> int:
    """
    Считает активные кошельки пользователя.
    """
    row = await db.execute(
        text(
            f"""
            SELECT COUNT(*)
              FROM {CORE_SCHEMA}.user_wallets
             WHERE user_id = :uid AND is_active IS TRUE
            """
        ),
        {"uid": user_id},
    )
    r = row.fetchone()
    return int(r[0]) if r and r[0] is not None else 0


async def _get_request_for_update(
    db: AsyncSession,
    user_id: int,
    nonce: str,
) -> Optional[Dict[str, Any]]:
    """
    Возвращает последнюю заявку по (user_id, nonce) с блокировкой FOR UPDATE.

    ИИ-защита:
      • Если одновременно придут несколько одинаковых транзакций с одним nonce,
        row-level lock не позволит двум воркерам одновременно менять одну и ту же заявку.
    """
    row = await db.execute(
        text(
            f"""
            SELECT id, user_id, candidate_addr, nonce, status, expires_at, tx_hash
              FROM {CORE_SCHEMA}.wallet_bind_requests
             WHERE user_id = :uid AND nonce = :nonce
             ORDER BY created_at DESC
             FOR UPDATE
             LIMIT 1
            """
        ),
        {"uid": user_id, "nonce": nonce},
    )
    r = row.fetchone()
    if not r:
        return None
    return {
        "id": int(r[0]),
        "user_id": int(r[1]),
        "candidate_addr": str(r[2]),
        "nonce": str(r[3]),
        "status": str(r[4]),
        "expires_at": r[5],
        "tx_hash": r[6],
    }
