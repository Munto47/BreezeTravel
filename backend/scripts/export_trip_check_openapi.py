from __future__ import annotations

import argparse
from importlib import import_module
import json
import os
import sys
from pathlib import Path
from typing import Any

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

REPO_ROOT = Path(__file__).resolve().parents[2]
# ``openapi.json`` is the immutable 99-path G01 compatibility snapshot.
# The generated client follows the additive, current contract instead.
TARGET = REPO_ROOT / "packages" / "trip-check-client" / "openapi.current.json"


def _normalize_schema(value: Any, *, path: tuple[str, ...] = ()) -> Any:
    """Remove Pydantic 2.9/2.10 singleton-schema rendering drift."""

    if isinstance(value, list):
        return [_normalize_schema(item, path=path) for item in value]
    if not isinstance(value, dict):
        return value

    normalized = {
        key: _normalize_schema(item, path=(*path, str(key)))
        for key, item in value.items()
    }
    constant = normalized.get("const")
    enum = normalized.get("enum")
    if isinstance(enum, list) and enum == [constant] and "const" in normalized:
        is_component_schema = (
            len(path) == 3
            and path[0] == "components"
            and path[1] == "schemas"
        )
        if is_component_schema:
            normalized.pop("const")
        else:
            normalized.pop("enum")
    return normalized


def _load_public_app():
    # Contract generation must never inherit a developer profile: that would
    # publish frozen room/import/audit routes through the shared client.
    os.environ.update(
        {
            "RUNTIME_PROFILE": "public",
            "JWT_SECRET_KEY": "openapi-only-jwt-secret-000000000001",
            "TRIP_UNDERSTANDING_COOKIE_SIGNING_KEY": "openapi-only-cookie-secret-00000001",
            "TRIP_UNDERSTANDING_SOURCE_ENCRYPTION_KEY": "openapi-only-source-secret-00000001",
        }
    )
    return import_module("app.main").app


def rendered_schema() -> str:
    app = _load_public_app()
    schema = _normalize_schema(app.openapi())
    return json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Export or verify the shared FastAPI OpenAPI contract")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--check", action="store_true")
    args = parser.parse_args()

    rendered = rendered_schema()
    if args.write:
        TARGET.parent.mkdir(parents=True, exist_ok=True)
        TARGET.write_text(rendered, encoding="utf-8", newline="\n")
        print(f"wrote {TARGET}")
        return 0

    if not TARGET.exists() or TARGET.read_text(encoding="utf-8") != rendered:
        print(f"OpenAPI contract drift: run python {Path(__file__).name} --write")
        return 1
    print(f"OpenAPI contract matches {TARGET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
