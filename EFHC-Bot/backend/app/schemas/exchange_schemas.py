"""Exchange schemas."""

from pydantic import BaseModel


class ExchangeSchema(BaseModel):
    """Placeholder exchange schema."""

    amount: float | None = None
