"""Shop schemas."""

from pydantic import BaseModel


class ShopItemSchema(BaseModel):
    """Placeholder shop item schema."""

    id: int | None = None
