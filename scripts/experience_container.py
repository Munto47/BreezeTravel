"""Compose entrypoint: apply existing schema, then serve the small runtime."""
import asyncio
import os
import sys

from experience import ROOT, migrate

if __name__ == "__main__":
    try:
        asyncio.run(migrate({}, dsn=os.environ["DATABASE_URL"]))
    except Exception:
        sys.exit("Database initialization unavailable; existing data retained.")
    os.chdir(ROOT / "backend")
    sys.path.insert(0, str(ROOT / "backend"))
    import uvicorn
    uvicorn.run("app.experience_main:app", host="0.0.0.0", port=8006, access_log=False)
