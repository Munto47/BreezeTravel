"""Read-only, deploy-safe access to the committed evaluation evidence."""

import json
from pathlib import Path

from fastapi import APIRouter, HTTPException

router = APIRouter()

_MANIFEST = Path(__file__).resolve().parents[2] / "evidence" / "latest.json"


@router.get("/evidence/latest")
async def latest_evidence():
    """Return the public evidence manifest; no raw prompts, keys, or user data."""
    if not _MANIFEST.exists():
        raise HTTPException(status_code=404, detail="尚未发布评测证据")
    return json.loads(_MANIFEST.read_text(encoding="utf-8"))
