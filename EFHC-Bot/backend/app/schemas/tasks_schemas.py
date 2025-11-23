"""Tasks schemas."""

from pydantic import BaseModel


class TaskSchema(BaseModel):
    """Placeholder task schema."""

    id: int | None = None
