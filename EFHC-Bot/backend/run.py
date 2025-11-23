"""Application entry point for EFHC backend."""

from __future__ import annotations

import uvicorn

from app import create_app


def main() -> None:
    """Run the backend FastAPI server."""

    uvicorn.run(create_app(), host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
