"""Transactions schemas."""

from pydantic import BaseModel


class TransactionSchema(BaseModel):
    """Placeholder transaction schema."""

    id: int | None = None
