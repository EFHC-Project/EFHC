"""Планировщик генерации энергии каждые 10 минут."""

from __future__ import annotations

import asyncio

from ..core.database_core import lifespan_session
from ..core.logging_core import get_logger
from sqlalchemy import select

from ..crud.panels_crud import PanelsCRUD
from ..models import User
from ..services.energy_service import EnergyService

logger = get_logger(__name__)


async def run() -> None:
    """Запустить один тик генерации для всех пользователей."""

    async with lifespan_session() as session:
        users = await session.scalars(select(User))
        for user in users:
            panels = await PanelsCRUD(session).list_active(user.id)
            produced = EnergyService(user, panels).accrue(seconds=600)
            logger.info(
                "energy accrued",
                extra={"user_id": user.id, "panels": len(panels), "produced_kwh": str(produced)},
            )
        await session.commit()


if __name__ == "__main__":
    asyncio.run(run())
