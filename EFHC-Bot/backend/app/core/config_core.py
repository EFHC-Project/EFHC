"""Core configuration for EFHC Bot canonical stack v2.8.

The configuration centralises all environment-driven settings so that
services and routes avoid ad-hoc reads. Defaults are safe for local
usage and mirror the canonical constants demanded by EFHC.
"""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from typing import Literal, Sequence

from pydantic import BaseSettings, Field, PostgresDsn, validator

GEN_PER_SEC_BASE_KWH = Decimal("0.00000692")
GEN_PER_SEC_VIP_KWH = Decimal("0.00000741")


class CoreConfig(BaseSettings):
    """Application settings loaded from environment variables.

    The class exposes strongly-typed fields for database connectivity,
    TON integration, admin controls, and strictness toggles. Values are
    cached via :func:`get_core_config` to avoid repetitive parsing.
    """

    app_env: Literal["dev", "stage", "prod"] = Field(
        "dev", description="Runtime environment switch for feature toggles."
    )
    database_url: PostgresDsn = Field(..., env="DATABASE_URL")

    ton_wallet_address: str = Field(
        "UQAyCoxmxzb2D6cmlf4M8zWYFYkaQuHbN_dgH-IfraFP8QKW",
        description="Canonical TON wallet for incoming payments.",
    )
    vip_nft_collection: str = Field(
        "EQASPXkEI0NsZQzqkPjk6O_i752LfwSWRFT9WzDc2SJ2zgi0",
        description="Canonical TON NFT collection used for VIP verification.",
    )

    admin_telegram_ids: Sequence[int] = Field(
        default_factory=list,
        description="List of Telegram IDs with admin access to management APIs.",
    )
    admin_api_key: str = Field(
        "dev-admin-key",
        description="Server-side API key accepted by admin routes when provided via header.",
    )
    strict_idempotency: bool = Field(
        True,
        description="When true monetary operations enforce Idempotency-Key header strictly.",
    )
    strict_etag: bool = Field(
        True,
        description="When true GET endpoints return ETag headers for cache-friendly clients.",
    )
    scheduler_tick_minutes: int = Field(
        10,
        description="Scheduler tick cadence; canonical configuration runs every 10 minutes.",
    )

    class Config:
        env_prefix = "EFHC_"
        case_sensitive = False

    @validator("admin_telegram_ids", pre=True)
    def _parse_admin_ids(cls, value: str | Sequence[int]) -> Sequence[int]:
        """Allow comma-separated integers in configuration sources."""

        if isinstance(value, str):
            return tuple(
                int(item.strip())
                for item in value.split(",")
                if item.strip().lstrip("+-").isdigit()
            )
        return tuple(value)


@lru_cache(maxsize=1)
def get_core_config() -> CoreConfig:
    """Return cached :class:`CoreConfig` instance.

    Using an LRU cache avoids repeated environment parsing while keeping
    the interface import-friendly for modules that need configuration.
    """

    return CoreConfig()
