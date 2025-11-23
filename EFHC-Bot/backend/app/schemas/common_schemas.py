"""Common schemas."""

from pydantic import BaseModel


class MessageSchema(BaseModel):
    """Placeholder message schema."""

    detail: str | None = None
