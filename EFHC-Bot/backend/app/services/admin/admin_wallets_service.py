# -*- coding: utf-8 -*-
# backend/app/services/admin/admin_wallets_service.py
# =============================================================================
# EFHC Bot — Кошельки пользователей (просмотр / привязка / отладка)
# -----------------------------------------------------------------------------
# Назначение:
#   • Безопасная работа с таблицей crypto_wallets:
#       - просмотр кошельков по внутреннему user_id и по Telegram ID;
#       - привязка нового кошелька к пользователю;
#       - смена основного (primary) кошелька;
#       - мягкая самодиагностика (health) по кошелькам.
#
# Жёсткие инварианты (канон):
#   • НИКАКИХ денежных операций здесь нет.
#     Только метаданные: chain / address / флаг is_primary.
#   • Любые денежные потоки (EFHC, kWh) проходят ТОЛЬКО через банковский сервис
#     и efhc_transfers_log. Этот модуль не трогает балансы.
#   • Привязка кошелька:
#       - допускается несколько кошельков на пользователя;
#       - только один может быть is_primary=TRUE;
#       - повторная привязка того же (user_id, chain, address) — идемпотентна.
#   • Администратор не может «тихо» отобрать кошелёк у другого пользователя:
#       - при попытке привязать address, уже принадлежащий другому user_id,
#         сервис поднимет WalletOwnershipError;
#       - намеренная смена владельца возможна только через отдельный метод
#         reassign_wallet(...) с ролью SuperAdmin.
#
# ИИ-защита:
#   • Валидация входных данных (обязательные поля, нормализация chain/address).
#   • Явные ошибки высокого уровня: WalletServiceError / UserNotFoundError /
#     WalletOwnershipError — для дружественных сообщений в UI.
#   • Идемпотентность:
#       - bind_wallet(...) безопасно при повторном вызове с теми же данными;
#       - при конфликте уникальности на INSERT мы делаем SELECT и возвращаем
#         уже существующую запись, не создавая дубликатов.
#   • Health-снимок выявляет базовые аномалии:
#       - пользователи без primary-кошелька при наличии кошельков;
#       - адреса, привязанные более чем к одному user_id (потенциальный баг).
# =============================================================================

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field, validator
from sqlalchemy import text
from sqlalchemy.engine import Result
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config_core import get_settings
from backend.app.core.logging_core import get_logger
from backend.app.core.utils_core import utcnow
from backend.app.services.admin.admin_rbac import AdminUser, AdminRole, RBAC
from backend.app.services.admin.admin_logging import AdminLogger

logger = get_logger(__name__)
S = get_settings()

SCHEMA_CORE: str = getattr(S, "DB_SCHEMA_CORE", "efhc_core") or "efhc_core"


# =============================================================================
# Исключения сервиса
# =============================================================================

class WalletServiceError(Exception):
    """Базовая ошибка кошелькового сервиса (для понятных ответов в UI)."""


class UserNotFoundError(WalletServiceError):
    """Пользователь не найден по user_id или telegram_id."""


class WalletNotFoundError(WalletServiceError):
    """Кошелёк не найден."""


class WalletOwnershipError(WalletServiceError):
    """
    Адрес уже занят другим пользователем.
    Используется, чтобы не позволить «тихий» перенос кошелька без осознанного решения.
    """


# =============================================================================
# DTO-модели
# =============================================================================

class WalletInfo(BaseModel):
    """Краткое описание кошелька для отображения в админке."""
    id: int
    user_id: int
    telegram_id: Optional[int] = Field(default=None, description="Внутренний Telegram ID пользователя, если есть")
    chain: str
    address: str
    is_primary: bool
    created_at: str


class BindWalletRequest(BaseModel):
    """
    Запрос на привязку кошелька.

    Правила:
      • Должен быть указан либо user_id, либо telegram_id.
      • chain/address нормализуются (обрезка пробелов, приведение chain к lower).
      • set_primary=True — кошелёк станет основным (остальные у пользователя
        будут автоматически сброшены в is_primary=FALSE).
      • force_reassign запрещён в этом запросе — смена владельца вынесена
        в отдельный метод reassign_wallet(...).
    """
    user_id: Optional[int] = Field(default=None, description="Внутренний ID пользователя")
    telegram_id: Optional[int] = Field(default=None, description="Telegram ID пользователя")
    chain: str = Field(..., min_length=1, max_length=32)
    address: str = Field(..., min_length=10, max_length=255)
    set_primary: bool = Field(default=True)

    @validator("chain", pre=True)
    def _norm_chain(cls, v: Any) -> str:
        return str(v).strip().lower()

    @validator("address", pre=True)
    def _norm_address(cls, v: Any) -> str:
        return str(v).strip()

    @validator("user_id", "telegram_id", always=True)
    def _check_user_or_telegram(cls, _v: Any, values: Dict[str, Any]) -> Any:
        uid = values.get("user_id")
        tid = values.get("telegram_id")
        if uid is None and tid is None:
            raise ValueError("Необходимо указать user_id или telegram_id")
        return _v


class ReassignWalletRequest(BaseModel):
    """
    Осознанный перенос кошелька с одного пользователя на другого.
    Доступен только SuperAdmin.
    """
    wallet_id: int
    new_user_id: Optional[int] = None
    new_telegram_id: Optional[int] = None

    @validator("new_user_id", "new_telegram_id", always=True)
    def _check_new_user(cls, _v: Any, values: Dict[str, Any]) -> Any:
        uid = values.get("new_user_id")
        tid = values.get("new_telegram_id")
        if uid is None and tid is None:
            raise ValueError("Необходимо указать new_user_id или new_telegram_id")
        return _v


class WalletsHealthSnapshot(BaseModel):
    """Самодиагностика по кошелькам для админ-дэшборда/отладки."""
    users_with_wallets_without_primary: int
    duplicated_addresses_across_users: int
    total_wallets: int
    total_users_with_wallets: int


# =============================================================================
# Внутренние хелперы
# =============================================================================

async def _resolve_user_id(
    db: AsyncSession,
    *,
    user_id: Optional[int],
    telegram_id: Optional[int],
) -> int:
    """
    Преобразует (user_id / telegram_id) → внутренний user_id.

    Инвариант:
      • если переданы оба — приоритет у user_id (telegram_id используется только для проверки).
      • если пользователь не найден — поднимает UserNotFoundError.
    """
    if user_id is not None:
        # Проверяем, что такой user_id существует (и, по возможности, что telegram_id совпадает).
        r: Result = await db.execute(
            text(
                f"""
                SELECT id, telegram_id
                FROM {SCHEMA_CORE}.users
                WHERE id = :uid
                LIMIT 1
                """
            ),
            {"uid": user_id},
        )
        row = r.fetchone()
        if not row:
            raise UserNotFoundError(f"Пользователь с id={user_id} не найден")
        if telegram_id is not None and row.telegram_id is not None and int(row.telegram_id) != int(telegram_id):
            logger.warning(
                "admin_wallets_service._resolve_user_id: несоответствие telegram_id "
                "(ожидали %s, в БД %s) для user_id=%s",
                telegram_id,
                row.telegram_id,
                user_id,
            )
        return int(row.id)

    # user_id нет, но есть telegram_id
    r2: Result = await db.execute(
        text(
            f"""
            SELECT id
            FROM {SCHEMA_CORE}.users
            WHERE telegram_id = :tid
            LIMIT 1
            """
        ),
        {"tid": telegram_id},
    )
    row2 = r2.fetchone()
    if not row2:
        raise UserNotFoundError(f"Пользователь с telegram_id={telegram_id} не найден")
    return int(row2.id)


async def _augment_with_telegram_id(db: AsyncSession, user_ids: List[int]) -> Dict[int, Optional[int]]:
    """
    Возвращает отображение user_id → telegram_id для списка пользователей.
    Используется для того, чтобы не тянуть telegram_id в основном запросе crypto_wallets.
    """
    if not user_ids:
        return {}
    r: Result = await db.execute(
        text(
            f"""
            SELECT id, telegram_id
            FROM {SCHEMA_CORE}.users
            WHERE id = ANY(:uids)
            """
        ),
        {"uids": user_ids},
    )
    mapping: Dict[int, Optional[int]] = {}
    for row in r.fetchall():
        mapping[int(row.id)] = int(row.telegram_id) if row.telegram_id is not None else None
    return mapping


# =============================================================================
# AdminWalletsService
# =============================================================================

class AdminWalletsService:
    """
    Сервис работы с кошельками пользователей.

    Важное:
      • все методы только ЧИТАЮТ/ИЗМЕНЯЮТ записи в crypto_wallets;
      • НИКАКИХ операций с EFHC/kWh здесь нет;
      • критические действия логируются через AdminLogger.
    """

    # -------------------------------------------------------------------------
    # Просмотр кошельков
    # -------------------------------------------------------------------------

    @staticmethod
    async def list_user_wallets_by_user_id(db: AsyncSession, user_id: int) -> List[WalletInfo]:
        """
        Возвращает список кошельков пользователя по внутреннему user_id.
        """
        # Подтверждаем, что пользователь существует (ИИ-защита от "висячих" id)
        _ = await _resolve_user_id(db, user_id=user_id, telegram_id=None)

        r: Result = await db.execute(
            text(
                f"""
                SELECT id, user_id, chain, address, is_primary, created_at
                FROM {SCHEMA_CORE}.crypto_wallets
                WHERE user_id = :uid
                ORDER BY is_primary DESC, id DESC
                """
            ),
            {"uid": user_id},
        )
        rows = r.fetchall()
        tg_map = await _augment_with_telegram_id(db, [user_id] if rows else [])

        out: List[WalletInfo] = []
        for row in rows:
            out.append(
                WalletInfo(
                    id=int(row.id),
                    user_id=int(row.user_id),
                    telegram_id=tg_map.get(int(row.user_id)),
                    chain=str(row.chain),
                    address=str(row.address),
                    is_primary=bool(row.is_primary),
                    created_at=row.created_at.isoformat() if hasattr(row.created_at, "isoformat") else str(row.created_at),
                )
            )
        return out

    @staticmethod
    async def list_user_wallets_by_telegram_id(db: AsyncSession, telegram_id: int) -> List[WalletInfo]:
        """
        Возвращает список кошельков пользователя по Telegram ID.

        Используется, когда админ знает только Telegram-идентификатор (например,
        при разборе заявки или проверки VIP/NFT-покупок).
        """
        user_id = await _resolve_user_id(db, user_id=None, telegram_id=telegram_id)
        return await AdminWalletsService.list_user_wallets_by_user_id(db, user_id=user_id)

    # -------------------------------------------------------------------------
    # Привязка кошелька (идемпотентная)
    # -------------------------------------------------------------------------

    @staticmethod
    async def bind_wallet(db: AsyncSession, req: BindWalletRequest, admin: AdminUser) -> WalletInfo:
        """
        Привязывает кошелёк к пользователю (или возвращает уже существующую запись).

        Поведение:
          • Требует роль не ниже Moderator.
          • Идемпотентность:
              - если запись (user_id, chain, address) уже есть — возвращаем её;
              - если при INSERT возникает IntegrityError, повторно читаем запись
                и трактуем это как повторный вызов.
          • set_primary=True:
              - сбрасывает is_primary=FALSE у всех кошельков пользователя,
                затем делает новый кошелёк основным.
          • Если address уже привязан к ДРУГОМУ user_id — WalletOwnershipError.
            Это защита от «тихого» перехвата кошелька.
        """
        RBAC.require_role(admin, AdminRole.MODERATOR)

        # 1) Разрешаем user_id по необходимости
        user_id = await _resolve_user_id(
            db,
            user_id=req.user_id,
            telegram_id=req.telegram_id,
        )

        chain = req.chain
        address = req.address

        # 2) Проверяем, не занят ли этот address другим пользователем
        r_check: Result = await db.execute(
            text(
                f"""
                SELECT id, user_id
                FROM {SCHEMA_CORE}.crypto_wallets
                WHERE chain = :chain AND address = :addr
                ORDER BY id ASC
                LIMIT 1
                """
            ),
            {"chain": chain, "addr": address},
        )
        addr_row = r_check.fetchone()
        if addr_row and int(addr_row.user_id) != user_id:
            # Адрес уже принадлежит другому пользователю — это НЕ idempotent-кейс,
            # а потенциальная ошибка данных или попытка перехвата.
            raise WalletOwnershipError(
                f"Адрес кошелька уже привязан к другому пользователю (user_id={addr_row.user_id})"
            )

        # 3) Проверим, нет ли уже такого кошелька у этого пользователя (чистый idempotent)
        if addr_row and int(addr_row.user_id) == user_id:
            # Уже существует — обновим is_primary при необходимости и вернём
            wallet_id = int(addr_row.id)

            if req.set_primary:
                # Снимаем primary со всех кошельков пользователя и выставляем на данном
                await db.execute(
                    text(
                        f"""
                        UPDATE {SCHEMA_CORE}.crypto_wallets
                        SET is_primary = FALSE
                        WHERE user_id = :uid
                        """
                    ),
                    {"uid": user_id},
                )
                await db.execute(
                    text(
                        f"""
                        UPDATE {SCHEMA_CORE}.crypto_wallets
                        SET is_primary = TRUE
                        WHERE id = :wid
                        """
                    ),
                    {"wid": wallet_id},
                )

            # Читаем актуальную запись
            r_w: Result = await db.execute(
                text(
                    f"""
                    SELECT id, user_id, chain, address, is_primary, created_at
                    FROM {SCHEMA_CORE}.crypto_wallets
                    WHERE id = :wid
                    LIMIT 1
                    """
                ),
                {"wid": wallet_id},
            )
            row = r_w.fetchone()
            if not row:
                raise WalletNotFoundError("Кошелёк исчез после проверки (рассинхронизация данных)")

            tg_map = await _augment_with_telegram_id(db, [user_id])
            return WalletInfo(
                id=int(row.id),
                user_id=int(row.user_id),
                telegram_id=tg_map.get(int(row.user_id)),
                chain=str(row.chain),
                address=str(row.address),
                is_primary=bool(row.is_primary),
                created_at=row.created_at.isoformat() if hasattr(row.created_at, "isoformat") else str(row.created_at),
            )

        # 4) Вставляем новый кошелёк
        try:
            # Если должен стать primary — сначала снимаем флаг со старых
            if req.set_primary:
                await db.execute(
                    text(
                        f"""
                        UPDATE {SCHEMA_CORE}.crypto_wallets
                        SET is_primary = FALSE
                        WHERE user_id = :uid
                        """
                    ),
                    {"uid": user_id},
                )

            r_insert: Result = await db.execute(
                text(
                    f"""
                    INSERT INTO {SCHEMA_CORE}.crypto_wallets
                        (user_id, chain, address, is_primary, created_at)
                    VALUES
                        (:uid, :chain, :addr, :is_primary, NOW() AT TIME ZONE 'UTC')
                    RETURNING id, user_id, chain, address, is_primary, created_at
                    """
                ),
                {
                    "uid": user_id,
                    "chain": chain,
                    "addr": address,
                    "is_primary": req.set_primary,
                },
            )
            row_ins = r_insert.fetchone()
        except IntegrityError as e:
            # Типичный случай — уникальный индекс на (user_id, chain, address).
            logger.warning("AdminWalletsService.bind_wallet: IntegrityError, пробуем прочитать существующий кошелёк: %s", e)
            r_exist: Result = await db.execute(
                text(
                    f"""
                    SELECT id, user_id, chain, address, is_primary, created_at
                    FROM {SCHEMA_CORE}.crypto_wallets
                    WHERE user_id = :uid AND chain = :chain AND address = :addr
                    LIMIT 1
                    """
                ),
                {"uid": user_id, "chain": chain, "addr": address},
            )
            row_ins = r_exist.fetchone()
        except SQLAlchemyError as e:
            logger.error("AdminWalletsService.bind_wallet: SQLAlchemyError: %s", e)
            raise WalletServiceError("Не удалось привязать кошелёк из-за ошибки базы данных")

        if not row_ins:
            raise WalletServiceError("Не удалось создать или прочитать запись кошелька")

        # Логируем действие админа
        await AdminLogger.write(
            db,
            admin_id=admin.id,
            action="BIND_WALLET",
            entity="crypto_wallet",
            entity_id=int(row_ins.id),
            details=f"user_id={user_id}, chain={chain}, address={address}",
        )

        tg_map = await _augment_with_telegram_id(db, [user_id])
        return WalletInfo(
            id=int(row_ins.id),
            user_id=int(row_ins.user_id),
            telegram_id=tg_map.get(int(row_ins.user_id)),
            chain=str(row_ins.chain),
            address=str(row_ins.address),
            is_primary=bool(row_ins.is_primary),
            created_at=row_ins.created_at.isoformat() if hasattr(row_ins.created_at, "isoformat") else str(row_ins.created_at),
        )

    # -------------------------------------------------------------------------
    # Смена владельца кошелька (осознанный reassign, только SuperAdmin)
    # -------------------------------------------------------------------------

    @staticmethod
    async def reassign_wallet_owner(db: AsyncSession, req: ReassignWalletRequest, admin: AdminUser) -> WalletInfo:
        """
        Переназначает кошелёк на другого пользователя.

        Использовать ТОЛЬКО в случаях:
          • явная ошибка данных (адрес записан не на того пользователя);
          • вручную подтверждённый кейс слияния аккаунтов.

        Защита:
          • доступен только SuperAdmin;
          • новый пользователь должен существовать;
          • логируется в admin_logs как WALLET_REASSIGN.
        """
        RBAC.require_role(admin, AdminRole.SUPERADMIN)
        new_user_id = await _resolve_user_id(
            db,
            user_id=req.new_user_id,
            telegram_id=req.new_telegram_id,
        )

        # Читаем текущую запись кошелька
        r: Result = await db.execute(
            text(
                f"""
                SELECT id, user_id, chain, address, is_primary, created_at
                FROM {SCHEMA_CORE}.crypto_wallets
                WHERE id = :wid
                LIMIT 1
                """
            ),
            {"wid": req.wallet_id},
        )
        row = r.fetchone()
        if not row:
            raise WalletNotFoundError(f"Кошелёк с id={req.wallet_id} не найден")

        old_user_id = int(row.user_id)

        # Переназначаем user_id (primary-флаг не трогаем, но можно при необходимости
        # потом вызвать bind_wallet или set_primary отдельно).
        await db.execute(
            text(
                f"""
                UPDATE {SCHEMA_CORE}.crypto_wallets
                SET user_id = :new_uid
                WHERE id = :wid
                """
            ),
            {"new_uid": new_user_id, "wid": req.wallet_id},
        )

        await AdminLogger.write(
            db,
            admin_id=admin.id,
            action="WALLET_REASSIGN",
            entity="crypto_wallet",
            entity_id=req.wallet_id,
            details=f"old_user_id={old_user_id}, new_user_id={new_user_id}",
        )

        tg_map = await _augment_with_telegram_id(db, [new_user_id])
        # Возвращаем обновлённую запись
        r2: Result = await db.execute(
            text(
                f"""
                SELECT id, user_id, chain, address, is_primary, created_at
                FROM {SCHEMA_CORE}.crypto_wallets
                WHERE id = :wid
                LIMIT 1
                """
            ),
            {"wid": req.wallet_id},
        )
        row2 = r2.fetchone()
        if not row2:
            raise WalletNotFoundError("Кошелёк исчез после переназначения")

        return WalletInfo(
            id=int(row2.id),
            user_id=int(row2.user_id),
            telegram_id=tg_map.get(int(row2.user_id)),
            chain=str(row2.chain),
            address=str(row2.address),
            is_primary=bool(row2.is_primary),
            created_at=row2.created_at.isoformat() if hasattr(row2.created_at, "isoformat") else str(row2.created_at),
        )

    # -------------------------------------------------------------------------
    # Смена основного кошелька
    # -------------------------------------------------------------------------

    @staticmethod
    async def set_primary_wallet(db: AsyncSession, wallet_id: int, admin: AdminUser) -> None:
        """
        Делает указанный кошелёк основным у его владельца:
          • сбрасывает is_primary=FALSE у всех кошельков этого пользователя;
          • устанавливает is_primary=TRUE для wallet_id.
        """
        RBAC.require_role(admin, AdminRole.MODERATOR)

        # Найдём кошелёк и его владельца
        r: Result = await db.execute(
            text(
                f"""
                SELECT id, user_id
                FROM {SCHEMA_CORE}.crypto_wallets
                WHERE id = :wid
                LIMIT 1
                """
            ),
            {"wid": wallet_id},
        )
        row = r.fetchone()
        if not row:
            raise WalletNotFoundError(f"Кошелёк с id={wallet_id} не найден")

        uid = int(row.user_id)

        # Сбрасываем primary у всех кошельков пользователя
        await db.execute(
            text(
                f"""
                UPDATE {SCHEMA_CORE}.crypto_wallets
                SET is_primary = FALSE
                WHERE user_id = :uid
                """
            ),
            {"uid": uid},
        )
        # Устанавливаем primary на выбранном кошельке
        await db.execute(
            text(
                f"""
                UPDATE {SCHEMA_CORE}.crypto_wallets
                SET is_primary = TRUE
                WHERE id = :wid
                """
            ),
            {"wid": wallet_id},
        )

        await AdminLogger.write(
            db,
            admin_id=admin.id,
            action="SET_PRIMARY_WALLET",
            entity="crypto_wallet",
            entity_id=wallet_id,
            details=f"user_id={uid}",
        )

    # -------------------------------------------------------------------------
    # Health / самодиагностика
    # -------------------------------------------------------------------------

    @staticmethod
    async def health_snapshot(db: AsyncSession) -> WalletsHealthSnapshot:
        """
        Лёгкий health-чек по кошелькам:
          • users_with_wallets_without_primary — пользователи, у которых есть
            хотя бы один кошелёк, но ни одного с is_primary=TRUE;
          • duplicated_addresses_across_users — количество адресов, которые
            привязаны более чем к одному user_id (анти-инвариант);
          • total_wallets — всего записей в crypto_wallets;
          • total_users_with_wallets — сколько пользователей имеют хотя бы один кошелёк.
        """
        users_without_primary = 0
        duplicated_addresses = 0
        total_wallets = 0
        total_users_with_wallets = 0

        # Всего кошельков и пользователей с кошельками
        try:
            r1: Result = await db.execute(
                text(
                    f"""
                    SELECT
                        COUNT(*) AS total_wallets,
                        COUNT(DISTINCT user_id) AS users_with_wallets
                    FROM {SCHEMA_CORE}.crypto_wallets
                    """
                )
            )
            row1 = r1.fetchone()
            if row1:
                total_wallets = int(row1.total_wallets or 0)
                total_users_with_wallets = int(row1.users_with_wallets or 0)
        except Exception as e:
            logger.error("AdminWalletsService.health_snapshot: ошибка агрегации total_wallets/users: %s", e)

        # Пользователи с кошельками, но без primary
        try:
            r2: Result = await db.execute(
                text(
                    f"""
                    SELECT COUNT(*) AS cnt
                    FROM (
                        SELECT user_id,
                               SUM(CASE WHEN is_primary THEN 1 ELSE 0 END) AS primary_cnt
                        FROM {SCHEMA_CORE}.crypto_wallets
                        GROUP BY user_id
                    ) t
                    WHERE t.primary_cnt = 0
                    """
                )
            )
            row2 = r2.fetchone()
            if row2:
                users_without_primary = int(row2.cnt or 0)
        except Exception as e:
            logger.error("AdminWalletsService.health_snapshot: ошибка вычисления users_without_primary: %s", e)

        # Адреса, привязанные к нескольким пользователям
        try:
            r3: Result = await db.execute(
                text(
                    f"""
                    SELECT COUNT(*) AS cnt
                    FROM (
                        SELECT address
                        FROM {SCHEMA_CORE}.crypto_wallets
                        GROUP BY address
                        HAVING COUNT(DISTINCT user_id) > 1
                    ) t
                    """
                )
            )
            row3 = r3.fetchone()
            if row3:
                duplicated_addresses = int(row3.cnt or 0)
        except Exception as e:
            logger.error("AdminWalletsService.health_snapshot: ошибка вычисления duplicated_addresses: %s", e)

        return WalletsHealthSnapshot(
            users_with_wallets_without_primary=users_without_primary,
            duplicated_addresses_across_users=duplicated_addresses,
            total_wallets=total_wallets,
            total_users_with_wallets=total_users_with_wallets,
        )


__all__ = [
    "WalletServiceError",
    "UserNotFoundError",
    "WalletNotFoundError",
    "WalletOwnershipError",
    "WalletInfo",
    "BindWalletRequest",
    "ReassignWalletRequest",
    "WalletsHealthSnapshot",
    "AdminWalletsService",
]

