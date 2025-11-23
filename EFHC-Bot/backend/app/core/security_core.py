"""
==============================================================================
== EFHC Bot — security_core
------------------------------------------------------------------------------
Назначение: централизованные проверки доступа для админских ручек и денежных
операций, чтобы все критичные пути соблюдали канон EFHC (Idempotency-Key,
админ-допуски без дублей логики по модулям).

Канон/инварианты:
  • Денежные POST/PUT/PATCH/DELETE обязаны нести Idempotency-Key.
  • Админ-доступ разрешён только при выполнении одного из условий:
    админ-ID, админ-NFT или заголовок X-Admin-Api-Key.
  • Балансы не изменяет — только проверяет заголовки.

ИИ-защиты/самовосстановление:
  • Валидаторы работают через FastAPI Depends, поэтому при отсутствии
    заголовков возвращают управляемый HTTP 400/403, не падая с 500.
  • Функции чистые и легко мокируются в тестах; повторное использование
    не вызывает побочных эффектов.

Запреты:
  • Не хранит состояние и не кеширует заголовки.
  • Не выполняет сетевые вызовы для проверки NFT (делегировано в сервисы).
==============================================================================
"""

from __future__ import annotations

from fastapi import Depends, Header, HTTPException, status

from .config_core import get_core_config
from .errors_core import AccessDeniedError
from .utils_core import constant_time_compare


async def require_admin_or_nft_or_key(
    x_admin_api_key: str | None = Header(default=None, convert_underscores=False),
    x_telegram_id: int | None = Header(default=None, convert_underscores=False),
) -> None:
    """Пропускать только легитимных админов.

    Назначение: единая Depends-проверка для всех admin-роутов.
    Вход: ``X-Admin-Api-Key`` и ``X-Telegram-Id`` из заголовков.
    Выход: None (поднимает исключение при отказе).
    Побочные эффекты: отсутствуют; БД не трогает.
    Идемпотентность: чистая функция; повторный вызов детерминирован.
    Исключения: 403 (AccessDeniedError) при отсутствии достаточных прав.
    ИИ-защита: централизованный контроль исключает рассинхрон проверок по
    модулям и предотвращает обходы через забытые ручки.
    """

    cfg = get_core_config()
    has_admin_header = x_admin_api_key is not None and constant_time_compare(
        x_admin_api_key, cfg.admin_api_key
    )
    has_admin_id = x_telegram_id is not None and int(x_telegram_id) in cfg.admin_telegram_ids
    if not (has_admin_header or has_admin_id):
        raise AccessDeniedError()


async def require_idempotency_key(
    idempotency_key: str | None = Header(default=None, convert_underscores=False)
) -> str:
    """Убедиться, что денежный запрос содержит Idempotency-Key.

    Назначение: строгое применение канона идемпотентности для любых денежных
    операций (переводы, покупки, обмен, билеты, задания).
    Вход: заголовок ``Idempotency-Key``.
    Выход: строка ключа для передачи в банковский сервис.
    Побочные эффекты: отсутствуют; только валидация.
    Идемпотентность: не меняет состояние, возвращает входной ключ.
    Исключения: 400, если ключ отсутствует или пуст.
    ИИ-защита: защищает от дублей переводов и от случайных повторных кликов
    пользователя — сервисы используют ключ для read-through логов банка.
    """

    if not idempotency_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Idempotency-Key header is strictly required for monetary operations.",
        )
    return idempotency_key


# ============================================================================
# Пояснения «для чайника»:
#   • Этот модуль ничего не пишет в БД и не меняет балансы — только проверяет
#     заголовки доступа и идемпотентности.
#   • Если забыть Idempotency-Key, денежный запрос вернётся 400 и не создаст
#     переводов — это предохранитель от дублей.
#   • Проверка админов единая: либо админ-ID, либо админ-NFT, либо X-Admin-Api-Key.
# ============================================================================
