"""Verify the hash-bound Trip Check V1 in-progress manifest without release claims.

The filename is retained for compatibility with existing local commands.  New
callers should treat the result as an authority/in-progress verification,
not as dual-entry delivery acceptance.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent
DEFAULT_LATEST = BACKEND / "evidence" / "releases" / "latest.json"
EXPECTED_MIGRATION = "027_trip_intake_revision_lineage.sql"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as stream:
        return json.load(stream)


def resolve_manifest(latest: dict[str, Any]) -> Path:
    reference = Path(str(latest["manifest"]))
    kind = latest.get("manifest_reference_kind", "workspace_relative")
    if kind == "workspace_relative":
        return ROOT / reference
    if kind == "absolute_external" and reference.is_absolute():
        return reference
    raise ValueError(f"unsupported manifest reference: {kind}")


def verify(latest_path: Path = DEFAULT_LATEST) -> dict[str, Any]:
    latest = read_json(latest_path)
    manifest_path = resolve_manifest(latest)
    if not manifest_path.is_file():
        raise ValueError(f"manifest does not exist: {manifest_path}")
    payload = read_json(manifest_path)

    if payload.get("schema_version") != "4.0":
        raise ValueError("expected Trip Check V1 baseline manifest schema 4.0")
    if payload.get("release_status") != "trip_check_v1_p3_input_provider_draft":
        raise ValueError("manifest is not a Trip Check V1 P3 input/provider draft record")
    if payload.get("latest_migration") != EXPECTED_MIGRATION:
        raise ValueError(f"manifest does not bind migration {EXPECTED_MIGRATION}")
    if payload.get("release_approval_granted") is not False:
        raise ValueError("in-progress manifest must not grant release approval")

    authority = payload.get("product_authority")
    if not isinstance(authority, dict):
        raise ValueError("product authority section is missing")
    for name in (
        "agents",
        "project_charter",
        "trip_check_spec",
        "trip_check_api_contract",
        "portfolio_mission",
        "program",
        "current_goal",
        "roadmap",
        "release_gates",
        "capability_status",
    ):
        reference = authority.get(name)
        if not isinstance(reference, dict):
            raise ValueError(f"missing authority reference: {name}")
        path = ROOT / str(reference.get("path", ""))
        if not path.is_file() or reference.get("sha256") != sha256_file(path):
            raise ValueError(f"authority hash mismatch: {name}")

    gates = payload.get("trip_check_v1_release_gate_evidence")
    if not isinstance(gates, dict):
        raise ValueError("Trip Check V1 release gate section is missing")
    if gates.get("overall_release_decision") != "REJECT":
        raise ValueError("in-progress manifest must keep the V1 release decision REJECT")
    if gates.get("g6_release_manifest") != "BASELINE_ONLY":
        raise ValueError("in-progress manifest must remain baseline-only")
    if gates.get("automated_proxy_judge") != "NOT_RUN":
        raise ValueError("automated proxy Judge state was overstated")
    if not gates.get("release_blockers"):
        raise ValueError("in-progress manifest must carry release blockers")
    reliability = gates.get("p2_reliability_gate")
    if not isinstance(reliability, dict) or not reliability.get("exists"):
        raise ValueError("P2 Reliability Gate evidence is missing")
    reliability_path = ROOT / str(reliability.get("path", ""))
    if reliability.get("sha256") != sha256_file(reliability_path):
        raise ValueError("P2 Reliability Gate evidence hash mismatch")
    if gates.get("p2_reliability_status") != "PASS_CONTROLLED_POSTGRES_BROWSER":
        raise ValueError("P2 Reliability Gate state was not preserved")

    legacy = payload.get("legacy_dual_entry_delivery_evidence")
    if not isinstance(legacy, dict):
        raise ValueError("legacy evidence index is missing")
    if legacy.get("human_validated") is not False or legacy.get("publicly_verified") is not False:
        raise ValueError("legacy evidence must not assert human or public validation")

    return {
        "status": "TRIP_CHECK_V1_IN_PROGRESS_EVIDENCE_VALID",
        "release_id": payload["release_id"],
        "manifest": str(manifest_path),
        "latest_migration": EXPECTED_MIGRATION,
        "human_validated": False,
        "publicly_verified": False,
        "overall_release_decision": gates["overall_release_decision"],
        "release_blockers": gates["release_blockers"],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--latest", type=Path, default=DEFAULT_LATEST)
    args = parser.parse_args()
    print(json.dumps(verify(args.latest), ensure_ascii=False))
