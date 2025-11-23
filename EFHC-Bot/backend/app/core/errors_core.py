"""Domain-specific exceptions aligned with EFHC canon v2.8."""

from __future__ import annotations

# ======================================================================
# EFHC Bot — core/errors_core.py
# ----------------------------------------------------------------------
# Назначение: единый набор HTTP-исключений для канона EFHC (денежные
#             заголовки, доступы, бизнес-ограничения). Модуль не меняет
#             балансы и не содержит бизнес-логики.
# Канон/инварианты:
#   • Денежные POST без Idempotency-Key блокируются IdempotencyError.
#   • Админ-доступ сверяется вне модуля, но ошибки стандартизированы.
# ИИ-защеты/самовосстановление:
#   • Чёткие тексты ошибок позволяют фронту/боту корректно ретраить без
#     неопределённости.
# Запреты:
#   • Нет P2P, нет EFHC→kWh; здесь только ошибки, не операции.
# ======================================================================

from fastapi import HTTPException, status


class IdempotencyError(HTTPException):
    """Поднимается при отсутствии/некорректности Idempotency-Key."""

    def __init__(
        self,
        detail: str = (
            "Idempotency-Key header is strictly required for monetary operations."
        ),
    ):
        super().__init__(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


class AccessDeniedError(HTTPException):
    """Поднимается при отказе в админ-доступе."""

    def __init__(self, detail: str = "Admin access required."):
        super().__init__(status_code=status.HTTP_403_FORBIDDEN, detail=detail)


class BusinessRuleError(HTTPException):
    """Поднимается при нарушении бизнес-инвариантов EFHC."""

    def __init__(self, detail: str = "Business rule violation"):
        super().__init__(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


# ======================================================================
# Пояснения «для чайника»:
#   • Эти исключения ничего не меняют в БД; они только нормализуют ответы.
#   • IdempotencyError возвращает 400, если нет Idempotency-Key у денежного
#     запроса.
#   • AccessDeniedError возвращает 403 для защищённых admin-ручек.
#   • BusinessRuleError сигнализирует о нарушении канонических правил EFHC.
# ======================================================================
