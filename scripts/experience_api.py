"""Serve the local experience API with a Psycopg-compatible Windows loop."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    if os.name == "nt":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    os.chdir(ROOT / "backend")
    sys.path.insert(0, str(ROOT / "backend"))
    import uvicorn

    uvicorn.run(
        "app.experience_main:app",
        host="127.0.0.1",
        port=int(os.environ.get("EXPERIENCE_API_PORT", "8006")),
        access_log=False,
    )


if __name__ == "__main__":
    main()
