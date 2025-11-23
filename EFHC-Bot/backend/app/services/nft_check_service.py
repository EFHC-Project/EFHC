# -*- coding: utf-8 -*-
# backend/app/services/nft_check_service.py
# =============================================================================
# Назначение кода:
#   Проверка VIP-статуса пользователя по наличию NFT из канонической
#   TON-коллекции в его кошельке (users.ton_wallet), с кэшем, логами и
#   безопасной синхронизацией флага users.is_vip.
#
# Канон/инварианты:
#   • VIP определяется ТОЛЬКО фактом наличия NFT в заданной коллекции TON.
#   • Покупка NFT в Shop — отдельный процесс, авто-минтинг запрещён.
#   • Денежные операции здесь НЕ выполняются (балансы не изменяются).
#
# ИИ-защита/самовосстановление:
#   • Кэш jsonb в БД с TTL снижает нагрузку на внешний индексатор.
#   • Мягкие фолбэки: отсутствие кэша/таблиц → безопасный пропуск.
#   • Пакетные функции для планировщика и админки (ежедневная проверка).
#
# Таблицы (минимум):
#   {SCHEMA_CORE}.users(
#       id, telegram_id, ton_wallet,
#       is_vip, vip_since, vip_checked_at, updated_at, ...
#   )
#
#   {SCHEMA_ADMIN}.nft_check_cache_ton(
#       address TEXT PRIMARY KEY,
#       assets_json JSONB,
#       snapshot_at TIMESTAMPTZ
#   )
#
#   {SCHEMA_ADMIN}.vip_status_log(
#       id BIGSERIAL PRIMARY KEY,
#       user_id BIGINT NOT NULL,
#       old_flag BOOLEAN,
#       new_flag BOOLEAN NOT NULL,
#       created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
#   )
#
# Таблицы логов/кэша допускаются как вспомогательные. Если их нет – сервис
# работает без них (просто без кэша/логов).
# =============================================================================

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.engine import Result
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config_core import get_settings
from backend.app.core.logging_core import get_logger
from backend.app.database import async_session_maker  # для run_daily_vip_check

logger = get_logger(__name__)
S = get_settings()

# -----------------------------------------------------------------------------
# Конфигурация (.env)
# -----------------------------------------------------------------------------
SCHEMA_CORE: str = getattr(S, "DB_SCHEMA_CORE", "efhc_core") or "efhc_core"
SCHEMA_ADMIN: str = getattr(S, "DB_SCHEMA_ADMIN", SCHEMA_CORE) or SCHEMA_CORE  # опционально

TON_API_BASE: str = str(getattr(S, "TON_API_URL", "https://tonapi.io/v2") or "https://tonapi.io/v2")
TON_API_KEY: str = str(getattr(S, "TON_API_KEY", "") or "")

# Каноническая коллекция VIP (TON)
TON_NFT_COLLECTION: str = str(
    getattr(S, "TON_NFT_COLLECTION", getattr(S, "GETGEMS_COLLECTION", "")) or ""
)

# TTL кэша (сек)
NFT_CACHE_TTL_SECONDS: int = int(getattr(S, "NFT_CACHE_TTL_SECONDS", 900) or 900)  # 15 минут

# Размеры батчей для high-level функций
VIP_CHECK_BATCH_SIZE: int = int(getattr(S, "NFT_CHECK_BATCH_SIZE", 500) or 500)

# -----------------------------------------------------------------------------
# DTO и служебные структуры
# -----------------------------------------------------------------------------

class NftAsset(BaseModel):
    """Упрощённое описание NFT из коллекции TON."""
    collection_address: str
    token_id: str
    name: Optional[str] = None
    raw: Optional[Dict[str, Any]] = None  # исходная запись индексатора (для отладки)


class NftCheckResult(BaseModel):
    """Результат проверки VIP для пользователя."""
    user_id: int
    ton_wallet: str
    is_vip: bool
    vip_assets: List[NftAsset] = Field(default_factory=list)
    all_assets: List[NftAsset] = Field(default_factory=list)
    checked_at: datetime = Field(default_factory=datetime.utcnow)
    source: str = Field(default="LIVE", description="LIVE | CACHE")


class NftCheckError(RuntimeError):
    """Исключение уровня сервиса проверки NFT (не деньги)."""


@dataclass
class VipBatchStats:
    total: int
    changed: int
    # errors не считаем, так как NftCheckService сам логирует ошибки и не поднимает их наверх


# -----------------------------------------------------------------------------
# HTTP-клиент (динамически, чтобы не требовать зависимости, если не используется)
# -----------------------------------------------------------------------------

async def _http_get_json(
    url: str,
    headers: Optional[Dict[str, str]] = None,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Выполняет GET и возвращает JSON.
    Если httpx отсутствует — подсказываем установить, но не роняем всю систему.
    """
    try:
        import httpx  # type: ignore
    except Exception as exc:  # noqa: BLE001
        raise NftCheckError(
            "Зависимость httpx не установлена. Добавьте 'httpx[http2]' в зависимости."
        ) from exc

    try:
        async with httpx.AsyncClient(timeout=15.0, http2=True) as client:  # type: ignore
            r = await client.get(url, headers=headers, params=params)
            r.raise_for_status()
            return r.json()  # type: ignore[no-any-return]
    except Exception as exc:  # noqa: BLE001
        raise NftCheckError(f"Ошибка TON API: {exc}") from exc


# -----------------------------------------------------------------------------
# Кэш (jsonb) — таблица {SCHEMA_ADMIN}.nft_check_cache_ton
#   PRIMARY KEY (address)
#   columns: address TEXT, assets_json JSONB, snapshot_at TIMESTAMPTZ
# -----------------------------------------------------------------------------

class _Cache:
    @staticmethod
    async def _table_exists(db: AsyncSession) -> bool:
        r: Result = await db.execute(
            text(
                """
                SELECT 1
                FROM information_schema.tables
                WHERE table_schema = :schema AND table_name = :table
                LIMIT 1
                """
            ),
            {"schema": SCHEMA_ADMIN, "table": "nft_check_cache_ton"},
        )
        return r.fetchone() is not None

    @staticmethod
    async def try_read(db: AsyncSession, address: str) -> Optional[List[Dict[str, Any]]]:
        """Вернуть кэш, если не просрочен; иначе None."""
        if not await _Cache._table_exists(db):
            return None
        min_ts = datetime.utcnow() - timedelta(seconds=NFT_CACHE_TTL_SECONDS)
        r: Result = await db.execute(
            text(
                f"""
                SELECT assets_json
                FROM {SCHEMA_ADMIN}.nft_check_cache_ton
                WHERE address = :addr AND snapshot_at >= :min_ts
                LIMIT 1
                """
            ),
            {"addr": address, "min_ts": min_ts},
        )
        row = r.fetchone()
        return (row.assets_json if row else None)  # type: ignore[attr-defined, no-any-return]

    @staticmethod
    async def write(db: AsyncSession, address: str, assets_json: List[Dict[str, Any]]) -> None:
        """Записать/обновить кэш. Если таблицы нет — безопасно пропустить."""
        if not await _Cache._table_exists(db):
            return
        await db.execute(
            text(
                f"""
                INSERT INTO {SCHEMA_ADMIN}.nft_check_cache_ton (address, assets_json, snapshot_at)
                VALUES (:addr, CAST(:assets AS jsonb), NOW() AT TIME ZONE 'UTC')
                ON CONFLICT (address) DO UPDATE
                SET assets_json = EXCLUDED.assets_json,
                    snapshot_at = EXCLUDED.snapshot_at
                """
            ),
            {"addr": address, "assets": _json_dumps(assets_json)},
        )


# -----------------------------------------------------------------------------
# Основной сервис проверки NFT/VIP
# -----------------------------------------------------------------------------

class NftCheckService:
    """
    Публичное API низкого уровня:
      • check_user_vip(db, user_id, force_refresh=False) -> NftCheckResult|None
        (НЕ меняет БД, только обращается к TON API/кэшу)
      • get_wallet_for_user(db, user_id) -> str|None
      • sync_users_is_vip(db, user_ids) -> dict[user_id, bool]
        (массовая синхронизация флага is_vip, с логом при изменениях).
    """

    # -------------------- Доступ к кошельку пользователя --------------------

    @staticmethod
    async def get_wallet_for_user(db: AsyncSession, user_id: int) -> Optional[str]:
        """
        Возвращает users.ton_wallet как основной кошелёк для проверки VIP.
        """
        r: Result = await db.execute(
            text(
                f"""
                SELECT ton_wallet
                FROM {SCHEMA_CORE}.users
                WHERE id = :uid
                LIMIT 1
                """
            ),
            {"uid": user_id},
        )
        row = r.fetchone()
        wallet = str(row.ton_wallet) if row and row.ton_wallet else None  # type: ignore[attr-defined]
        if wallet:
            wallet = wallet.strip()
        return wallet or None

    # ---------------------------- Проверка VIP ------------------------------

    @staticmethod
    async def check_user_vip(
        db: AsyncSession,
        user_id: int,
        force_refresh: bool = False,
    ) -> Optional[NftCheckResult]:
        """
        Что делает:
          • Берёт users.ton_wallet, если нет — возвращает None (не ошибка).
          • Пытается прочитать кэш (если force_refresh=False).
          • При необходимости делает запрос в TON API → кэширует результат.
          • Считает VIP = наличие токена из TON_NFT_COLLECTION.
        Исключения:
          • NftCheckError — проблемы с HTTP/зависимостями (логируется, прокидывается дальше).
        """
        wallet = await NftCheckService.get_wallet_for_user(db, user_id)
        if not wallet:
            return None
        all_assets: List[NftAsset]
        source = "LIVE"

        if not force_refresh:
            cached = await _Cache.try_read(db, wallet)
            if cached is not None:
                try:
                    all_assets = [NftAsset(**a) for a in cached]  # type: ignore[list-item]
                    source = "CACHE"
                except Exception:  # noqa: BLE001
                    # Повреждённый кэш — просто перезапросим
                    all_assets = await _fetch_ton_assets(wallet)
                    await _Cache.write(db, wallet, [a.dict() for a in all_assets])
            else:
                all_assets = await _fetch_ton_assets(wallet)
                await _Cache.write(db, wallet, [a.dict() for a in all_assets])
        else:
            all_assets = await _fetch_ton_assets(wallet)
            await _Cache.write(db, wallet, [a.dict() for a in all_assets])

        vip_assets = _filter_vip_assets(all_assets, TON_NFT_COLLECTION)
        is_vip = bool(vip_assets)
        return NftCheckResult(
            user_id=user_id,
            ton_wallet=wallet,
            is_vip=is_vip,
            vip_assets=vip_assets,
            all_assets=all_assets,
            checked_at=datetime.utcnow(),
            source=source,
        )

    # ----------------------- Массовая проверка для cron ---------------------

    @staticmethod
    async def sync_users_is_vip(
        db: AsyncSession,
        user_ids: Sequence[int],
        force_refresh: bool = False,
        log_table: str = "vip_status_log",
    ) -> Dict[int, bool]:
        """
        Для списка пользователей:
          • Проверяет VIP,
          • Обновляет users.is_vip, vip_since, vip_checked_at,
          • (опционально) записывает лог изменений в {SCHEMA_ADMIN}.vip_status_log (если таблица существует).
        Возвращает словарь {user_id: is_vip}.
        """
        out: Dict[int, bool] = {}
        for uid in user_ids:
            try:
                res = await NftCheckService.check_user_vip(db, uid, force_refresh=force_refresh)
                if res is None:
                    # Нет кошелька — снимем VIP (False)
                    before = await _read_users_is_vip(db, uid)
                    updated = await _write_users_is_vip(db, uid, False)
                    out[uid] = False
                    if updated and before is not None and before is not False:
                        await _try_write_vip_log(db, uid, old=before, new=False, table=log_table)
                    continue

                # Установим флаг по факту
                before = await _read_users_is_vip(db, uid)
                updated = await _write_users_is_vip(db, uid, res.is_vip)
                out[uid] = res.is_vip
                if updated and before is not None and before != res.is_vip:
                    await _try_write_vip_log(db, uid, old=before, new=res.is_vip, table=log_table)
            except NftCheckError as e:
                # Ошибка внешнего сервиса — не роняем цикл, просто логируем
                logger.warning("VIP check failed for user_id=%s: %s", uid, e)
            except Exception as e:  # noqa: BLE001
                logger.exception("VIP check unexpected error for user_id=%s: %s", uid, e)
        return out


# -----------------------------------------------------------------------------
# Вспомогательные функции (SQL/TON API)
# -----------------------------------------------------------------------------

async def _fetch_ton_assets(address: str) -> List[NftAsset]:
    """
    Забирает NFT для TON-адреса и нормализует в список NftAsset.
    """
    if not TON_NFT_COLLECTION:
        # Коллекция не задана — нечего проверять (канон требует коллекцию)
        logger.warning("TON_NFT_COLLECTION не задан — VIP будет всегда False.")
    url = f"{TON_API_BASE.rstrip('/')}/accounts/{address}/nfts"
    headers: Dict[str, str] = {}
    if TON_API_KEY:
        headers["Authorization"] = f"Bearer {TON_API_KEY}"

    # Некоторые индексаторы поддерживают limit, collection и т.п.
    params: Dict[str, Any] = {"limit": 1000}
    try:
        data = await _http_get_json(url, headers=headers, params=params)
    except NftCheckError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise NftCheckError(f"TON API unexpected error: {exc}") from exc

    items = data.get("nft_items") or data.get("nfts") or []
    out: List[NftAsset] = []
    for item in items:
        # Нормализация полей под общий вид
        collection_addr = str(
            (item.get("collection") or {}).get("address")
            or item.get("collection_address")
            or ""
        )
        token_id = str(item.get("index") or item.get("id") or item.get("token_id") or "")
        name = None
        md = item.get("metadata")
        if isinstance(md, dict):
            n = md.get("name")
            if isinstance(n, str):
                name = n
        if not collection_addr or not token_id:
            continue
        out.append(
            NftAsset(
                collection_address=collection_addr,
                token_id=token_id,
                name=name,
                raw=item if isinstance(item, dict) else None,
            )
        )
    return out


def _filter_vip_assets(assets: List[NftAsset], vip_collection: str) -> List[NftAsset]:
    """Фильтрует активы по адресу целевой коллекции (TON)."""
    if not vip_collection:
        return []
    return [a for a in assets if a.collection_address == vip_collection]


def _json_dumps(obj: Any) -> str:
    """Безопасный json.dumps для записи в БД."""
    try:
        import json
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        return str(obj)


async def _read_users_is_vip(db: AsyncSession, user_id: int) -> Optional[bool]:
    r: Result = await db.execute(
        text(
            f"""
            SELECT is_vip
            FROM {SCHEMA_CORE}.users
            WHERE id = :uid
            LIMIT 1
            """
        ),
        {"uid": user_id},
    )
    row = r.fetchone()
    return (bool(row.is_vip) if row and row.is_vip is not None else None)  # type: ignore[attr-defined]


async def _write_users_is_vip(db: AsyncSession, user_id: int, flag: bool) -> bool:
    """
    Обновляет users.is_vip с учётом канона:
      • is_vip = :flag
      • vip_since:
          - если устанавливаем VIP (flag = TRUE) и ранее его не было или был False → NOW()
          - если снимаем VIP (flag = FALSE) → NULL
          - если флаг не меняется и VIP уже был → оставляем vip_since как есть
      • vip_checked_at всегда обновляем на NOW()
    Возвращает True, если строка обновлена (пользователь существует).
    """
    r: Result = await db.execute(
        text(
            f"""
            UPDATE {SCHEMA_CORE}.users
               SET is_vip = :flag,
                   vip_since = CASE
                                  WHEN :flag = TRUE
                                       AND (vip_since IS NULL OR is_vip = FALSE)
                                      THEN NOW() AT TIME ZONE 'UTC'
                                  WHEN :flag = FALSE
                                      THEN NULL
                                  ELSE vip_since
                               END,
                   vip_checked_at = NOW() AT TIME ZONE 'UTC',
                   updated_at     = NOW() AT TIME ZONE 'UTC'
             WHERE id = :uid
            """
        ),
        {"uid": user_id, "flag": flag},
    )
    return r.rowcount > 0


async def _try_write_vip_log(
    db: AsyncSession,
    user_id: int,
    old: Optional[bool],
    new: bool,
    table: str = "vip_status_log",
) -> None:
    """
    Опциональный лог изменений VIP в {SCHEMA_ADMIN}.vip_status_log (если таблица есть).
    Схема таблицы (пример):
        CREATE TABLE admin.vip_status_log (
            id BIGSERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL REFERENCES core.users(id) ON DELETE CASCADE,
            old_flag BOOLEAN,
            new_flag BOOLEAN NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
    """
    # Проверим наличие таблицы единожды на вызов (дешёвый запрос)
    r: Result = await db.execute(
        text(
            """
            SELECT 1
            FROM information_schema.tables
            WHERE table_schema = :schema AND table_name = :table
            LIMIT 1
            """
        ),
        {"schema": SCHEMA_ADMIN, "table": table},
    )
    if r.fetchone() is None:
        return
    await db.execute(
        text(
            f"""
            INSERT INTO {SCHEMA_ADMIN}.{table} (user_id, old_flag, new_flag, created_at)
            VALUES (:uid, :old, :new, NOW() AT TIME ZONE 'UTC')
            """
        ),
        {"uid": user_id, "old": old, "new": new},
    )


# -----------------------------------------------------------------------------
# Высокоуровневые функции (перенос утраченных сценариев контроля VIP)
# -----------------------------------------------------------------------------

async def _fetch_user_ids_with_wallet_batch(
    db: AsyncSession,
    *,
    offset: int,
    limit: int,
) -> List[int]:
    """
    Возвращает список user_id, у которых есть ton_wallet (батчами).
    """
    r: Result = await db.execute(
        text(
            f"""
            SELECT id
            FROM {SCHEMA_CORE}.users
            WHERE ton_wallet IS NOT NULL AND LENGTH(TRIM(ton_wallet)) > 0
            ORDER BY id ASC
            OFFSET :off
            LIMIT :lim
            """
        ),
        {"off": int(offset), "lim": int(limit)},
    )
    rows = r.fetchall()
    return [int(row.id) for row in rows]  # type: ignore[attr-defined]


async def check_user_vip_once(
    db: AsyncSession,
    *,
    user_id: int,
    force_refresh: bool = False,
) -> Optional[NftCheckResult]:
    """
    Упрощённый сценарий «проверить и сразу синхронизировать одного пользователя».

    Что делает:
      • вызывает NftCheckService.check_user_vip (TON API/кэш),
      • обновляет users.is_vip/vip_since/vip_checked_at,
      • при изменении флага пишет запись в vip_status_log (если таблица есть),
      • возвращает NftCheckResult или None (если кошелька нет).
    """
    try:
        res = await NftCheckService.check_user_vip(db, user_id, force_refresh=force_refresh)
    except NftCheckError as e:
        logger.warning("check_user_vip_once: external error for user_id=%s: %s", user_id, e)
        return None
    except Exception as e:  # noqa: BLE001
        logger.exception("check_user_vip_once: unexpected error for user_id=%s: %s", user_id, e)
        return None

    if res is None:
        # нет кошелька → снимаем VIP
        before = await _read_users_is_vip(db, user_id)
        updated = await _write_users_is_vip(db, user_id, False)
        if updated and before is not None and before is not False:
            await _try_write_vip_log(db, user_id, old=before, new=False)
        return None

    before = await _read_users_is_vip(db, user_id)
    updated = await _write_users_is_vip(db, user_id, res.is_vip)
    if updated and before is not None and before != res.is_vip:
        await _try_write_vip_log(db, user_id, old=before, new=res.is_vip)
    return res


async def check_users_subset(
    db: AsyncSession,
    *,
    user_ids: List[int],
    force_refresh: bool = False,
) -> VipBatchStats:
    """
    Перенос логики «проверить ограниченный список пользователей»:
      • Честно считает, у скольких людей реально изменился флаг is_vip.
      • Не возвращает искусственных счётчиков ошибок — ошибки логируются.
    """
    if not user_ids:
        return VipBatchStats(total=0, changed=0)

    total = 0
    changed = 0
    for uid in user_ids:
        before = await _read_users_is_vip(db, uid)
        res = await check_user_vip_once(db, user_id=uid, force_refresh=force_refresh)
        total += 1
        if before is None:
            # пользователя не было — не считаем как «изменение флага»
            continue
        after = await _read_users_is_vip(db, uid)
        if after is not None and before != after:
            changed += 1

    return VipBatchStats(total=total, changed=changed)


async def check_all_users_once(
    db: AsyncSession,
    *,
    batch_size: int = VIP_CHECK_BATCH_SIZE,
    force_refresh: bool = False,
) -> Dict[str, Any]:
    """
    Массовая проверка всех пользователей, у которых есть ton_wallet.
    Работает батчами, безопасно для планировщика.

    Возвращает:
      {
        "total": <сколько пользователей с кошельком обработано>,
        "changed": <у скольких реально сменился флаг is_vip>,
        "batches": <сколько батчей прошло>,
      }
    """
    offset = 0
    total = 0
    changed_total = 0
    batches = 0

    while True:
        ids = await _fetch_user_ids_with_wallet_batch(db, offset=offset, limit=int(batch_size))
        if not ids:
            break
        batches += 1
        # фиксируем "до"
        before_map: Dict[int, Optional[bool]] = {}
        for uid in ids:
            before_map[uid] = await _read_users_is_vip(db, uid)

        # синхронизация через низкоуровневый сервис
        await NftCheckService.sync_users_is_vip(db, ids, force_refresh=force_refresh)

        # фиксируем "после" и считаем изменения
        for uid in ids:
            after = await _read_users_is_vip(db, uid)
            if before_map.get(uid) is not None and after is not None and before_map[uid] != after:
                changed_total += 1

        total += len(ids)
        await db.commit()
        offset += len(ids)

    return {"total": total, "changed": changed_total, "batches": batches}


async def vip_health_snapshot(db: AsyncSession) -> Dict[str, Any]:
    """
    Лёгкий health-снимок:
      • сколько пользователей вообще привязали кошелёк;
      • сколько сейчас VIP.
    """
    row_all: Result = await db.execute(
        text(
            f"""
            SELECT COUNT(*)
            FROM {SCHEMA_CORE}.users
            WHERE ton_wallet IS NOT NULL AND LENGTH(TRIM(ton_wallet)) > 0
            """
        )
    )
    row_vip: Result = await db.execute(
        text(
            f"""
            SELECT COUNT(*)
            FROM {SCHEMA_CORE}.users
            WHERE is_vip = TRUE
            """
        )
    )
    total_with_wallet = int(row_all.scalar() or 0)
    total_vip = int(row_vip.scalar() or 0)
    return {
        "with_wallet": total_with_wallet,
        "vip": total_vip,
        "collection": TON_NFT_COLLECTION or "",
    }


async def run_daily_vip_check() -> Dict[str, Any]:
    """
    Удобная обёртка для планировщика:
      • открывает свою сессию,
      • запускает check_all_users_once,
      • коммитит изменения и пишет лог.
    """
    async with async_session_maker() as db:
        stats = await check_all_users_once(db, batch_size=VIP_CHECK_BATCH_SIZE, force_refresh=False)
        await db.commit()
        logger.info("VIP daily check finished: %s", stats)
        return stats


# =============================================================================
# Пояснения «для чайника»:
#   • NftCheckService.check_user_vip — низкоуровневый вызов (только API/кэш).
#   • sync_users_is_vip, check_user_vip_once, check_all_users_once —
#     сценарии, которые уже меняют users.is_vip/vip_since/vip_checked_at.
#   • vip_health_snapshot — быстрый health-отчёт для UI/админки.
#   • run_daily_vip_check — точка входа для cron/планировщика.
# =============================================================================

__all__ = [
    "NftAsset",
    "NftCheckResult",
    "NftCheckError",
    "NftCheckService",
    "check_user_vip_once",
    "check_users_subset",
    "check_all_users_once",
    "vip_health_snapshot",
    "run_daily_vip_check",
]
