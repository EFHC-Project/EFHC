"""Orders schemas."""

from pydantic import BaseModel


class OrderSchema(BaseModel):
    """Placeholder order schema."""

    id: int | None = None
