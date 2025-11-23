"""Domain-specific exceptions used across EFHC services."""

from __future__ import annotations

from fastapi import HTTPException, status


class IdempotencyError(HTTPException):
    """Raised when idempotency requirements are violated."""

    def __init__(self, detail: str = "Idempotency-Key header is strictly required for monetary operations."):
        super().__init__(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


class AccessDeniedError(HTTPException):
    """Raised when admin access is rejected."""

    def __init__(self, detail: str = "Admin access required."):
        super().__init__(status_code=status.HTTP_403_FORBIDDEN, detail=detail)


class BusinessRuleError(HTTPException):
    """Raised when business invariant is violated."""

    def __init__(self, detail: str = "Business rule violation"):
        super().__init__(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)
