"""Сервис проверки VIP NFT."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from ..core.logging_core import get_logger
from ..models import User

logger = get_logger(__name__)


class NFTCheckService:
    """Каркас для проверки наличия VIP NFT у пользователя."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def mark_vip(self, user: User, has_nft: bool) -> User:
        user.is_vip = has_nft
        logger.info("vip flag updated", extra={"user_id": user.id, "is_vip": has_nft})
        return user
