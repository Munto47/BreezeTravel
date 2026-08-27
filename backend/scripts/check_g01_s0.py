"""Machine-check the G01-S0 inventory and frozen compatibility boundary."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
INVENTORY_PATH = REPO_ROOT / "docs" / "governance" / "g01_s0_asset_disposition.json"
HTTP_METHODS = {"get", "post", "put", "patch", "delete"}


def load_inventory() -> dict[str, Any]:
    return json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))


def _relative(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def _surface_assets(name: str, config: dict[str, Any]) -> list[str]:
    if "assets" in config:
        return sorted(config["assets"])
    if name == "pages":
        return sorted(_relative(path) for path in (REPO_ROOT / config["root"]).rglob("page.tsx"))
    if name == "legacy_openapi_paths":
        payload = json.loads((REPO_ROOT / config["source"]).read_text(encoding="utf-8"))
        return sorted(payload["paths"])
    if name == "source_api_additions":
        current = json.loads((REPO_ROOT / config["source"]).read_text(encoding="utf-8"))
        legacy = json.loads(
            (REPO_ROOT / config["legacy_source"]).read_text(encoding="utf-8")
        )
        return sorted(set(current["paths"]) - set(legacy["paths"]))
    if name == "backend_modules":
        root = REPO_ROOT / config["root"]
        paths = [path for path in root.iterdir() if path.name != "__pycache__"]
        return sorted(_relative(path) for path in paths if path.is_dir() or path.suffix == ".py")
    if name == "backend_test_groups":
        return sorted(
            _relative(path)
            for path in (REPO_ROOT / config["root"]).glob(config["selector"])
        )
    raise ValueError(f"unknown inventory surface: {name}")


def _classify(asset: str, config: dict[str, Any], allowed: set[str]) -> str:
    matches = {
        rule["disposition"]
        for rule in config["rules"]
        if any(fnmatch.fnmatchcase(asset, pattern) for pattern in rule["patterns"])
    }
    if not matches and config.get("default_disposition"):
        matches.add(config["default_disposition"])
    if len(matches) != 1:
        raise AssertionError(f"{asset} must map to exactly one disposition, got {sorted(matches)}")
    disposition = matches.pop()
    if disposition not in allowed:
        raise AssertionError(f"{asset} uses unknown disposition {disposition}")
    return disposition


def _legacy_openapi_receipt(inventory: dict[str, Any]) -> dict[str, Any]:
    contract = inventory["legacy_openapi"]
    snapshot = REPO_ROOT / contract["snapshot"]
    raw = snapshot.read_bytes()
    payload = json.loads(raw)
    paths = payload["paths"]
    operation_count = sum(
        method.casefold() in HTTP_METHODS
        for item in paths.values()
        for method in item
    )
    actual = {
        "sha256": hashlib.sha256(raw).hexdigest(),
        "path_count": len(paths),
        "operation_count": operation_count,
    }
    expected = {key: contract[key] for key in actual}
    if actual != expected:
        raise AssertionError(f"legacy OpenAPI baseline drift: expected={expected}, actual={actual}")
    return actual


def frozen_changes(inventory: dict[str, Any] | None = None) -> list[str]:
    inventory = inventory or load_inventory()
    guard = inventory["frozen_guards"]
    command = [
        "git",
        "diff",
        "--name-only",
        guard["baseline"],
        "--",
        *guard["pathspecs"],
    ]
    result = subprocess.run(
        command,
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return sorted(line.strip() for line in result.stdout.splitlines() if line.strip())


def audit_inventory() -> dict[str, Any]:
    inventory = load_inventory()
    allowed = set(inventory["allowed_dispositions"])
    classified: dict[str, dict[str, str]] = {}
    summaries: dict[str, dict[str, int]] = {}
    for name, config in inventory["surfaces"].items():
        assets = _surface_assets(name, config)
        if "observed_count" in config and len(assets) != config["observed_count"]:
            raise AssertionError(
                f"{name} count drift: expected {config['observed_count']}, got {len(assets)}"
            )
        if len(assets) < config.get("observed_minimum_count", 0):
            raise AssertionError(f"{name} unexpectedly lost assets")
        classified[name] = {asset: _classify(asset, config, allowed) for asset in assets}
        summaries[name] = dict(sorted(Counter(classified[name].values()).items()))
    changes = frozen_changes(inventory)
    if changes:
        raise AssertionError(f"frozen G01-S0 assets changed since baseline: {changes}")
    return {
        "schema_version": "g01-s0-audit-receipt-v1",
        "goal_id": inventory["goal_id"],
        "baseline": inventory["baseline"],
        "legacy_openapi": _legacy_openapi_receipt(inventory),
        "surface_summaries": summaries,
        "classified_assets": classified,
        "frozen_diff": [],
        "status": "PASS",
    }


def main() -> None:
    print(json.dumps(audit_inventory(), ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
