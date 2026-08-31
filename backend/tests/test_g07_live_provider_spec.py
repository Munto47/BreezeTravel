from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals.g07_candidate.live_spec_builder import build_g07_live_provider_spec
from evals.trip_check_v1.p6.contracts_v1 import (
    P6ContractError,
    digest,
    validate_candidate_run_spec,
)


REPOSITORY_ROOT = Path(__file__).parents[2]
SUBJECT = "7" * 40


def _built(tmp_path: Path) -> dict:
    spec, path = build_g07_live_provider_spec(
        output_root=tmp_path / "inputs",
        repo_root=REPOSITORY_ROOT,
        formal=False,
        subject_commit=SUBJECT,
    )
    assert json.loads(path.read_text(encoding="utf-8")) == spec
    return spec


def _rehash(value: dict) -> dict:
    value["run_spec_hash"] = digest(
        {key: item for key, item in value.items() if key != "run_spec_hash"}
    )
    return value


def test_g07_live_adapter_binds_current_contract_and_migrations(tmp_path: Path) -> None:
    spec = _built(tmp_path)
    assert spec["subject_commit"] == SUBJECT
    assert spec["upstream_ref"] == "origin/codex/g07-candidate"
    assert spec["upstream_commit"] == SUBJECT
    assert spec["database"]["required_migration"] == (
        "034_trip_understanding_screenshot_batches.sql"
    )
    assert spec["provider_live_matrix"]["max_calls"] == 18
    assert spec["evidence_root"].endswith(f"/g07-candidate/{SUBJECT}")
    assert set(path.name for path in (tmp_path / "inputs").iterdir()) == {
        "candidate_run_spec.json",
        "g07_live_adapter_manifest.json",
        "migration_manifest.json",
    }
    migration = json.loads(
        (tmp_path / "inputs/migration_manifest.json").read_text(encoding="utf-8")
    )
    assert migration["file_count"] == 34
    assert migration["files"][-1]["path"].endswith(
        "034_trip_understanding_screenshot_batches.sql"
    )
    assert "credential" not in json.dumps(spec).casefold()


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        (
            "upstream_ref",
            "origin/codex/not-the-candidate",
            "P6_CANDIDATE_RUN_SPEC_SCHEMA_INVALID",
        ),
        (
            "evidence_root",
            f"D:/munto/code/claudeProject/agentTravel-p6-artifacts/p6-candidate/{SUBJECT}",
            "P6_EVIDENCE_ROOT_SUBJECT_BINDING_INVALID",
        ),
    ],
)
def test_g07_live_adapter_rejects_branch_or_evidence_root_drift(
    tmp_path: Path,
    field: str,
    value: str,
    reason: str,
) -> None:
    spec = _built(tmp_path)
    spec[field] = value
    with pytest.raises(P6ContractError, match=reason):
        validate_candidate_run_spec(_rehash(spec))


def test_g07_live_adapter_rejects_legacy_migration_binding(tmp_path: Path) -> None:
    spec = _built(tmp_path)
    spec["database"]["required_migration"] = (
        "025_miniapp_identity_and_upload_batches.sql"
    )
    with pytest.raises(P6ContractError, match="P6_CANDIDATE_MIGRATION_BINDING_INVALID"):
        validate_candidate_run_spec(_rehash(spec))


def test_g07_live_adapter_output_is_write_once(tmp_path: Path) -> None:
    _built(tmp_path)
    with pytest.raises(P6ContractError, match="G07_LIVE_SPEC_OUTPUT_NOT_EMPTY"):
        build_g07_live_provider_spec(
            output_root=tmp_path / "inputs",
            repo_root=REPOSITORY_ROOT,
            formal=False,
            subject_commit=SUBJECT,
        )
