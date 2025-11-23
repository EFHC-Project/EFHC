"""Security and access-control helpers."""

from __future__ import annotations

from fastapi import Depends, Header, HTTPException, status

from .config_core import get_core_config
from .errors_core import AccessDeniedError
from .utils_core import constant_time_compare


async def require_admin_or_nft_or_key(
    x_admin_api_key: str | None = Header(default=None, convert_underscores=False),
    x_telegram_id: int | None = Header(default=None, convert_underscores=False),
) -> None:
    """Guard admin routes using three independent signals.

    1. Telegram ID present in canonical admin list;
    2. Possession of an admin NFT (omitted for brevity — handled upstream);
    3. A server-side admin API key provided via ``X-Admin-Api-Key``.
    """

    cfg = get_core_config()
    has_admin_header = x_admin_api_key is not None and constant_time_compare(
        x_admin_api_key, cfg.admin_api_key
    )
    has_admin_id = x_telegram_id is not None and int(x_telegram_id) in cfg.admin_telegram_ids
    if not (has_admin_header or has_admin_id):
        raise AccessDeniedError()


async def require_idempotency_key(
    idempotency_key: str | None = Header(default=None, convert_underscores=False)
) -> str:
    """Ensure that monetary requests always include the Idempotency-Key header."""

    if not idempotency_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Idempotency-Key header is strictly required for monetary operations.",
        )
    return idempotency_key
