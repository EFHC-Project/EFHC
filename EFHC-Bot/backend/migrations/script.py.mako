# -*- coding: utf-8 -*-
"""Alembic revision template for EFHC Bot (canon v2.8).

Назначение:
    • Шаблон для будущих миграций Alembic.
    • Гарантирует единый стиль (PEP8, канон EFHC) и заполненные upgrade/downgrade.

Важно:
    • Не оставляйте миграции пустыми или с TODO — описывайте реальные изменения.
    • Денежная логика отсутствует; здесь только DDL.
"""

from __future__ import annotations

from typing import Sequence, Union  # noqa: F401 (используется в будущих миграциях)

from alembic import op  # noqa: F401 (заготовка для будущих операций)
import sqlalchemy as sa  # noqa: F401 (шаблон для будущих миграций)

# revision identifiers, used by Alembic.
revision: str = "<fill>"
down_revision: str | None = "<fill>"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Применить миграцию."""
    pass


def downgrade() -> None:
    """Откатить миграцию."""
    pass
