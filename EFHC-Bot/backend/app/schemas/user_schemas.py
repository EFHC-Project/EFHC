"""User schemas."""

from pydantic import BaseModel


class UserSchema(BaseModel):
    """Placeholder user schema."""

    id: int | None = None
