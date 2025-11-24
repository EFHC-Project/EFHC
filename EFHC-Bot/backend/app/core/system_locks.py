# -*- coding: utf-8 -*-
# backend/app/core/system_locks.py
# =============================================================================
# Назначение кода:
#   Единый «канон-замок» EFHC Bot. Гарантирует инварианты проекта до старта
#   приложения и во время работы:
#   • только per-sec ставки генерации;
#   • обмен строго kWh→EFHC 1:1;
#   • запрет P2P;
#   • запрет авто-выдачи NFT;
#   • обязательный Idempotency-Key для денежных запросов;
#   • запрет отрицательных балансов у пользователей.
#
# Канон / инварианты (фиксируем жёстко):
#   • GEN_PER_SEC_BASE_KWH и GEN_PER_SEC_VIP_KWH — единственные источники
#     истинных ставок; никаких «суточных» констант в коде быть не должно.
#   • EFHC_KWH_RATE == 1.0 навсегда. Обратной конверсии EFHC→kWh нет.
#   • Любое движение EFHC — только «Банк ↔ Пользователь». P2P запрещён.
#   • Пользователь НИКОГДА не уходит в минус (main/bonus).
#   • Банк МОЖЕТ быть в минусе (дефицит не блокирует операции).
#   • NFT не доставляются автоматически — только заявка и ручная обработка.
#   • Денежные POST/PUT/PATCH/DELETE — строго с заголовком Idempotency-Key.
#
# ИИ-защита / самовосстановление:
#   • Проверки канона выполняются на старте (init_system_locks) и при каждом
#     финансовом действии через публичные хелперы.
#   • Middleware может «прикрыть» все денежные ручки по префиксам или по
#     метке @monetary_op, даже если разработчик забыл зависимость в роуте.
#   • Все проверки формируют чёткие исключения LockViolation или HTTP 400–403
#     без падений процесса.
#
# Запреты:
#   • Здесь нет бизнес-логики денег/энергии. Только проверки и middleware.
#   • Никаких «суточных» (daily) ставок и VIP-множителей вне канона.
# =============================================================================

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from typing import Any, Callable, Iterable, Optional, Tuple

from fastapi import Header, HTTPException, Request, status
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from starlette.types import ASGIApp

from backend.app.core.config_core import get_settings
from backend.app.core.logging_core import get_logger

logger = get_logger(__name__)
settings = get_settings()

# -----------------------------------------------------------------------------
# Внутренние константы канона (сравниваем с .env)
# -----------------------------------------------------------------------------
Q8 = Decimal(1).scaleb(-8)  # точность Decimal(8), округление вниз

CANON_BASE = Decimal("0.00000692").quantize(Q8, rounding=ROUND_DOWN)
CANON_VIP = Decimal("0.00000741").quantize(Q8, rounding=ROUND_DOWN)
CANON_RATE = Decimal("1.0").quantize(Q8, rounding=ROUND_DOWN)


# -----------------------------------------------------------------------------
# Исключения и DTO для нарушений канона
# -----------------------------------------------------------------------------
class LockViolation(RuntimeError):
    """
    Нарушение канона / архитектурных запретов.

    Важно:
    • Это ОШИБКА ПРОЕКТА, а не «ошибка пользователя».
    • Должна отлавливаться верхним слоем и логироваться,
      но не маскироваться под 500 без объяснения.
    """


@dataclass(frozen=True)
class BalanceSnapshot:
    """
    Снимок пользовательского баланса для проверки «не уйти в минус».

    main:
        Основной баланс EFHC (панели, покупки, вывод).
    bonus:
        Бонусный баланс EFHC (рефералка, задания, лотереи и т.п.).
    """

    main: Decimal
    bonus: Decimal


# -----------------------------------------------------------------------------
# Публичные проверки — импортируются сервисами / роутами
# -----------------------------------------------------------------------------
def assert_per_sec_canon() -> None:
    """
    Проверяет, что в конфиге заданы канонические per-sec ставки генерации
    и фиксированный курс 1:1.

    Вызывается на старте (init_system_locks). При несоответствии поднимает
    LockViolation, чтобы не дать приложению запуститься в некорректном режиме.
    """
    try:
        base = Decimal(str(getattr(settings, "GEN_PER_SEC_BASE_KWH")))
        vip = Decimal(str(getattr(settings, "GEN_PER_SEC_VIP_KWH")))
        rate = Decimal(str(getattr(settings, "EFHC_KWH_RATE")))
    except Exception as exc:  # noqa: BLE001
        raise LockViolation(
            "Некорректные per-sec ставки или курс в настройках: "
            f"{exc}",
        ) from exc

    if base.quantize(Q8, rounding=ROUND_DOWN) != CANON_BASE:
        raise LockViolation(
            "GEN_PER_SEC_BASE_KWH≠"
            f"{CANON_BASE}. Найдено: {base}. Допустимо только "
            "каноническое значение.",
        )

    if vip.quantize(Q8, rounding=ROUND_DOWN) != CANON_VIP:
        raise LockViolation(
            "GEN_PER_SEC_VIP_KWH≠"
            f"{CANON_VIP}. Найдено: {vip}. Допустимо только "
            "каноническое значение.",
        )

    if rate.quantize(Q8, rounding=ROUND_DOWN) != CANON_RATE:
        raise LockViolation(
            "EFHC_KWH_RATE должен быть ровно 1.0. "
            f"Найдено: {rate}.",
        )


def assert_exchange_direction_kwh_to_efhc_only(
    *,
    reverse: bool = False,
) -> None:
    """
    Запрещает обратную конверсию EFHC→kWh.

    Сервис обмена должен вызывать без аргументов; передача reverse=True
    приводит к немедленной ошибке канона.
    """
    if reverse:
        raise LockViolation(
            "Обратная конверсия EFHC→kWh запрещена каноном EFHC.",
        )


def assert_p2p_forbidden(
    sender_user_id: Optional[int],
    receiver_user_id: Optional[int],
) -> None:
    """
    Запрещает любые прямые переводы user→user.

    Разрешены только операции «Банк↔Пользователь». Для банка следует
    передавать None вместо user_id.
    """
    if sender_user_id is not None and receiver_user_id is not None:
        raise LockViolation(
            "P2P операции между пользователями запрещены каноном: "
            "разрешены только «Банк↔Пользователь».",
        )


def assert_no_auto_nft_delivery(*, auto: bool = False) -> None:
    """
    Запрещает авто-выдачу NFT.

    Любая попытка авто-доставки (auto=True) — нарушение канона. Выдача
    VIP-NFT и других NFT должна идти только по заявке и ручному апруву.
    """
    if auto:
        raise LockViolation(
            "Автоматическая выдача NFT запрещена. "
            "Допустимы только заявки и ручная обработка.",
        )


def ensure_user_non_negative_after(
    before: BalanceSnapshot,
    delta_main: Decimal,
    delta_bonus: Decimal,
) -> None:
    """
    Проверка «не уйти в минус» для пользователя.

    Использование:
    • Вызывать ПЕРЕД применением списания.
    • Покупки за EFHC должны блокироваться, если расчёт даёт отрицательный итог.
    • Пополнения / конвертация kWh→EFHC не приводят к минусу и не блокируются.

    before:
        Текущий снимок баланса.
    delta_main:
        Планируемое изменение основного баланса (может быть отрицательным).
    delta_bonus:
        Планируемое изменение бонусного баланса (может быть отрицательным).
    """
    after_main = (before.main + delta_main).quantize(
        Q8,
        rounding=ROUND_DOWN,
    )
    after_bonus = (before.bonus + delta_bonus).quantize(
        Q8,
        rounding=ROUND_DOWN,
    )

    if after_main < 0 or after_bonus < 0:
        raise LockViolation(
            "Списание привело бы к отрицательному балансу пользователя: "
            f"main={after_main}, bonus={after_bonus}. Операция запрещена.",
        )


def flag_bank_deficit_if_negative(after_bank_balance: Decimal) -> None:
    """
    Логирует дефицит Банка EFHC, если баланс отрицательный.

    Важно:
    • Банк МОЖЕТ уходить в минус — это не ошибка и не блокирует операцию.
    • Хелпер для метрик/логов: позволяет отслеживать моменты,
      когда Банк выдаёт больше EFHC, чем получил.
    """
    if after_bank_balance < 0:
        logger.warning("BANK DEFICIT MODE: after=%s", str(after_bank_balance))


# -----------------------------------------------------------------------------
# Обязательный Idempotency-Key для денежных запросов
# -----------------------------------------------------------------------------
async def require_idempotency_key(
    idempotency_key: Optional[str] = Header(
        default=None,
        convert_underscores=False,
        alias="Idempotency-Key",
    ),
) -> str:
    """
    FastAPI-зависимость для денежных POST/PUT/PATCH/DELETE.

    Возвращает строку ключа, либо 400, если заголовок пустой или отсутствует.
    """
    if not idempotency_key or not idempotency_key.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Idempotency-Key header is strictly required for "
                "monetary operations."
            ),
        )
    return idempotency_key.strip()


def monetary_op(func: Callable[..., Any]) -> Callable[..., Any]:
    """
    Декоратор-метка для эндпоинтов, чтобы middleware знал, что это денежная
    операция.

    Пример:
        @router.post(
            "/buy",
            dependencies=[Depends(require_idempotency_key)],
        )
        @monetary_op
        async def buy_panel(...):
            ...
    """
    setattr(func, "_monetary_op", True)
    return func


class MonetaryIdempotencyMiddleware(BaseHTTPMiddleware):
    """
    Middleware-страховка: если разработчик забыл зависимость
    require_idempotency_key на «денежной ручке», этот слой проверит заголовок
    по префиксам пути ИЛИ по явной метке @monetary_op и вернёт 400 при
    отсутствии ключа.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        path_prefixes: Optional[Iterable[str]] = None,
        methods: Tuple[str, ...] = (
            "POST",
            "PUT",
            "PATCH",
            "DELETE",
        ),
    ) -> None:
        super().__init__(app)
        self.methods = methods

        # Конфигурируемые префиксы денежных зон:
        #   • можно передать в конструктор;
        #   • можно задать в settings.MONETARY_PATH_PREFIXES;
        #   • иначе используются дефолтные значения.
        default_prefixes: Tuple[str, ...] = (
            "/exchange",
            "/panels",
            "/shop",
            "/withdraw",
            "/admin",
        )

        if path_prefixes is not None:
            self.prefixes = tuple(path_prefixes)
        else:
            cfg = getattr(settings, "MONETARY_PATH_PREFIXES", None)
            if isinstance(cfg, (list, tuple)):
                self.prefixes = tuple(str(p) for p in cfg)
            else:
                self.prefixes = default_prefixes

    async def dispatch(  # type: ignore[override]
        self,
        request: Request,
        call_next: Callable[[Request], Any],
    ) -> Any:
        # Не проверяем безопасные методы.
        if request.method not in self.methods:
            return await call_next(request)

        path = request.url.path or "/"

        # 1) Если путь «денежный» по префиксу — требуем ключ.
        path_matches = any(path.startswith(p) for p in self.prefixes)

        # 2) Если эндпоинт помечен @monetary_op — тоже требуем.
        marked = False
        router = getattr(request.app, "router", None)
        if router is not None:
            for route in router.routes:
                route_path = getattr(route, "path", None)
                route_methods = getattr(route, "methods", None)
                if route_path != path or not route_methods:
                    continue
                endpoint = getattr(route, "endpoint", None)
                if endpoint and getattr(endpoint, "_monetary_op", False):
                    marked = True
                    break

        if path_matches or marked:
            idk = (request.headers.get("Idempotency-Key") or "").strip()
            if not idk:
                return _http_400(
                    "Idempotency-Key header is required "
                    "for monetary operations.",
                )

        return await call_next(request)


def _http_400(msg: str) -> JSONResponse:
    return _json_response(
        status.HTTP_400_BAD_REQUEST,
        {"detail": msg},
    )


def _json_response(code: int, payload: dict) -> JSONResponse:
    return JSONResponse(status_code=code, content=payload)


# -----------------------------------------------------------------------------
# Инициализация «замков» на старте приложения
# -----------------------------------------------------------------------------
def init_system_locks(app: ASGIApp) -> None:
    """
    Инициализация канон-проверок и middleware.

    Вызывать один раз при сборке FastAPI:
        app = FastAPI(...)
        init_system_locks(app)
    """
    # 1) Жёсткая проверка per-sec ставок и курса.
    assert_per_sec_canon()
    logger.info(
        "SystemLocks: per-sec canon and 1:1 rate validated.",
    )

    # 2) Подключаем middleware страховки Idempotency-Key.
    app.add_middleware(MonetaryIdempotencyMiddleware)
    logger.info(
        "SystemLocks: MonetaryIdempotencyMiddleware installed.",
    )

    # 3) (опционально) Логи о режиме банка.
    allow_negative_bank = bool(
        getattr(settings, "ALLOW_NEGATIVE_BANK_BALANCE", True),
    )
    if not allow_negative_bank:
        logger.warning(
            "ALLOW_NEGATIVE_BANK_BALANCE=false. Это противоречит канону "
            "(банку разрешён отрицательный баланс). "
            "Рекомендуется установить true.",
        )


__all__ = [
    "LockViolation",
    "BalanceSnapshot",
    "assert_per_sec_canon",
    "assert_exchange_direction_kwh_to_efhc_only",
    "assert_p2p_forbidden",
    "assert_no_auto_nft_delivery",
    "ensure_user_non_negative_after",
    "flag_bank_deficit_if_negative",
    "require_idempotency_key",
    "monetary_op",
    "MonetaryIdempotencyMiddleware",
    "init_system_locks",
]

# =============================================================================
# Пояснения «для чайника»:
#   • Сервисы должны вызывать ensure_user_non_negative_after(...) перед
#     любыми списаниями с пользовательских балансов. Если расчёт даёт минус —
#     поднимаем LockViolation и НЕ выполняем операцию.
#   • Для банка: после применения операции вызывайте
#     flag_bank_deficit_if_negative(...) чтобы фиксировать дефицит в логах.
#     Операция при этом не блокируется — это штатная ситуация.
#   • Денежные эндпоинты:
#       а) dependencies=[Depends(require_idempotency_key)];
#       б) декоратор-метка @monetary_op.
#     Даже если забыли — middleware проверит по префиксу пути.
#   • P2P: при любой попытке указать одновременно sender_user_id и
#     receiver_user_id вызывайте assert_p2p_forbidden(...), чтобы немедленно
#     поймать нарушение канона «нет внутренних переводов».
#   • NFT: авто-доставка запрещена — watcher / shop должны создавать заявки
#     со статусом наподобие PAID_PENDING_MANUAL, а админ-панель уже решает,
#     когда и кому фактически выдавать NFT.
# =============================================================================
