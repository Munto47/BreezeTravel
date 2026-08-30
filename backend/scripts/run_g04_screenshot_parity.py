from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pydantic import ValidationError

from evals.g04_screenshot import (
    G04ScreenshotManifestError,
    G04ScreenshotParityManifestV1,
    score_g04_screenshot_manifest,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Score an explicit G04 screenshot parity manifest without reading source images"
    )
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)

    try:
        raw = json.loads(args.manifest.read_text(encoding="utf-8"))
        manifest = G04ScreenshotParityManifestV1.model_validate(raw)
        report = score_g04_screenshot_manifest(manifest)
    except (OSError, json.JSONDecodeError, ValidationError, G04ScreenshotManifestError) as exc:
        print(
            json.dumps(
                {
                    "schema_version": "g04-screenshot-score-error-v1",
                    "status": "INVALID_MANIFEST",
                    "error_type": type(exc).__name__,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2

    payload = report.model_dump_json(indent=2)
    print(payload)
    if args.output is not None:
        args.output.write_text(f"{payload}\n", encoding="utf-8", newline="\n")
    return 0 if report.gate_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
