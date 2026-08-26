from __future__ import annotations

import argparse
from importlib import import_module
import json
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

app = import_module("app.main").app


REPO_ROOT = Path(__file__).resolve().parents[2]
TARGET = REPO_ROOT / "packages" / "trip-check-client" / "openapi.json"


def rendered_schema() -> str:
    return json.dumps(app.openapi(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"


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
