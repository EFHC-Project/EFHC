"""Rating schemas."""

from pydantic import BaseModel


class RatingSchema(BaseModel):
    """Placeholder rating schema."""

    score: int | None = None
