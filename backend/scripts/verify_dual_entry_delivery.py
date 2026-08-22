"""Verify the hash-bound local-delivery evidence without making release claims."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent
DEFAULT_LATEST = BACKEND / "evidence" / "releases" / "latest.json"
EXPECTED_MIGRATION = "021_atomic_suggestion_undo.sql"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as stream:
        return json.load(stream)


def resolve_manifest(latest_path: Path, latest: dict[str, Any]) -> Path:
    reference = Path(str(latest["manifest"]))
    kind = latest.get("manifest_reference_kind", "workspace_relative")
    if kind == "workspace_relative":
        return ROOT / reference
    if kind == "absolute_external" and reference.is_absolute():
        return reference
    raise ValueError(f"unsupported manifest reference: {kind}")


def verify(latest_path: Path = DEFAULT_LATEST) -> dict[str, Any]:
    latest = read_json(latest_path)
    manifest_path = resolve_manifest(latest_path, latest)
    if not manifest_path.is_file():
        raise ValueError(f"manifest does not exist: {manifest_path}")
    payload = read_json(manifest_path)

    if payload.get("schema_version") != "3.0":
        raise ValueError("expected dual-entry manifest schema 3.0")
    if payload.get("release_status") != "dual_entry_local_delivery_candidate":
        raise ValueError("manifest is not a dual-entry local delivery candidate")
    if payload.get("latest_migration") != EXPECTED_MIGRATION:
        raise ValueError(f"manifest does not bind migration {EXPECTED_MIGRATION}")
    if payload.get("release_approval_granted") is not False:
        raise ValueError("local delivery manifest must not grant release approval")

    delivery = payload.get("dual_entry_delivery_evidence")
    if not isinstance(delivery, dict):
        raise ValueError("dual-entry evidence section is missing")
    if delivery.get("m1_dev_evidence_type") != "synthetic_proxy":
        raise ValueError("M1-dev evidence must remain synthetic_proxy")
    if delivery.get("human_validated") is not False or delivery.get("publicly_verified") is not False:
        raise ValueError("local delivery manifest must not assert human or public validation")

    for name in (
        "final_plan",
        "capability_status",
        "local_delivery_acceptance",
        "m1_dev_dataset_manifest",
        "m1_dev_proxy_gate",
    ):
        reference = delivery.get(name)
        if not isinstance(reference, dict):
            raise ValueError(f"missing evidence reference: {name}")
        path = ROOT / str(reference.get("path", ""))
        if not path.is_file() or reference.get("sha256") != sha256_file(path):
            raise ValueError(f"evidence hash mismatch: {name}")

    release_gates = payload.get("dual_entry_release_gate_evidence")
    if not isinstance(release_gates, dict):
        raise ValueError("dual-entry release gate evidence index is missing")
    for name in (
        "dataset_manifest",
        "latest_import_http_gate",
        "latest_builder_http_gate",
        "g5_restart_evidence",
        "full_backend_junit",
    ):
        reference = release_gates.get(name)
        if not isinstance(reference, dict):
            raise ValueError(f"missing release gate evidence reference: {name}")
        if reference.get("exists") is not True:
            raise ValueError(f"release gate evidence is unavailable: {name}")
        path = ROOT / str(reference.get("path", ""))
        if not path.is_file() or reference.get("sha256") != sha256_file(path):
            raise ValueError(f"release gate evidence hash mismatch: {name}")
    if release_gates.get("overall_release_decision") != "REJECT":
        raise ValueError("unapproved local delivery must keep the release gate rejected")
    if release_gates.get("external_blind_bundle_provisioned") is not False:
        raise ValueError("external blind bundle state was overstated")
    if release_gates.get("human_calibration_case_count") != 0:
        raise ValueError("human calibration state was overstated")

    gate_path = ROOT / str(delivery["m1_dev_proxy_gate"]["path"])
    gate = read_json(gate_path)
    if gate.get("status") != "M1_DEV_PROXY_PASSED" or gate.get("passed") is not True:
        raise ValueError("M1-dev proxy gate is not passed")
    if gate.get("evidence_type") != "synthetic_proxy":
        raise ValueError("M1-dev proxy gate boundary changed")

    return {
        "status": "LOCAL_DELIVERY_EVIDENCE_VALID",
        "release_id": payload["release_id"],
        "manifest": str(manifest_path),
        "latest_migration": EXPECTED_MIGRATION,
        "human_validated": False,
        "publicly_verified": False,
        "overall_release_decision": release_gates["overall_release_decision"],
        "release_blockers": release_gates.get("release_blockers", []),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--latest", type=Path, default=DEFAULT_LATEST)
    args = parser.parse_args()
    print(json.dumps(verify(args.latest), ensure_ascii=False))
