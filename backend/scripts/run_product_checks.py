"""One checked-in suite manifest for local execution and CI."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "docs/testing/product_test_manifest.json"


def main() -> int:
    suites = json.loads(MANIFEST.read_text(encoding="utf-8"))["suites"]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("suite", choices=sorted(suites))
    parser.add_argument("--list", action="store_true")
    args, extra = parser.parse_known_args()
    extra = extra[1:] if extra[:1] == ["--"] else extra
    suite = suites[args.suite]
    cwd = ROOT / suite["directory"]
    for name in suite["files"]:
        path = (cwd / name).resolve()
        if not path.is_relative_to(cwd.resolve()) or not path.is_file():
            raise ValueError(f"Suite file is missing or outside its directory: {name}")
    if args.list:
        print("\n".join(suite["files"]))
        return 0
    if suite["runner"] == "pytest":
        command = [sys.executable, "-m", "pytest", "-q"]
    else:
        command = ["node", str(cwd / "node_modules/@playwright/test/cli.js"), "test",
                   "-c", suite.get("config", "playwright.experience.config.js")]
    return subprocess.call([*command, *suite["files"], *suite.get("args", []), *extra], cwd=cwd)


if __name__ == "__main__":
    raise SystemExit(main())
