"""Structured logging helpers for EFHC services."""

from __future__ import annotations

import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


def get_logger(name: str) -> logging.Logger:
    """Return module-specific logger."""

    return logging.getLogger(name)
