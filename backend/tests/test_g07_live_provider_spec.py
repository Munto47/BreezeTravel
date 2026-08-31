from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from evals.g07_candidate.live_provider_runner import run_g07_live_provider_gate
from evals.g07_candidate.live_spec_builder import (
    build_g07_live_provider_spec,
    validate_g07_live_provider_spec,
)
from evals.trip_check_v1.p6.contracts_v1 import P6ContractError, digest
from tests.test_trip_check_p6_live_provider_runner import (
    _fake_live_runner,
    _settings,
)


REPOSITORY_ROOT = Path(__file__).parents[2]
SUBJECT = "7" * 40
TREE = "8" * 40


def _built(tmp_path: Path) -> tuple[dict, Path]:
    spec, path = build_g07_live_provider_spec(
        output_root=tmp_path / "inputs",
        repo_root=REPOSITORY_ROOT,
        formal=False,
        subject_commit=SUBJECT,
        candidate_tree=TREE,
    )
    assert json.loads(path.read_text(encoding="utf-8")) == spec
    return spec, path


def _rehash(value: dict) -> dict:
    value["run_spec_hash"] = digest(
        {key: item for key, item in value.items() if key != "run_spec_hash"}
    )
    return value


def test_g07_live_spec_binds_current_contract_and_migrations(tmp_path: Path) -> None:
    spec, path = _built(tmp_path)
    assert spec["subject_commit"] == SUBJECT
    assert spec["candidate_tree"] == TREE
    assert spec["upstream_ref"] == "origin/codex/g07-candidate"
    assert spec["migration"]["latest"] == (
        "034_trip_understanding_screenshot_batches.sql"
    )
    assert spec["provider_live_matrix"]["max_calls"] == 18
    assert spec["evidence_root"].endswith(f"/g07-candidate/{SUBJECT}")
    assert path.name == "g07_live_provider_spec.json"
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
            "G07_LIVE_SPEC_BINDING_INVALID",
        ),
        (
            "evidence_root",
            f"D:/munto/code/claudeProject/agentTravel-p6-artifacts/p6-candidate/{SUBJECT}",
            "G07_LIVE_SPEC_EVIDENCE_ROOT_INVALID",
        ),
    ],
)
def test_g07_live_spec_rejects_branch_or_evidence_root_drift(
    tmp_path: Path,
    field: str,
    value: str,
    reason: str,
) -> None:
    spec, _path = _built(tmp_path)
    spec[field] = value
    with pytest.raises(P6ContractError, match=reason):
        validate_g07_live_provider_spec(_rehash(spec))


def test_g07_live_spec_rejects_legacy_migration_binding(tmp_path: Path) -> None:
    spec, _path = _built(tmp_path)
    spec["migration"]["latest"] = "025_miniapp_identity_and_upload_batches.sql"
    with pytest.raises(P6ContractError, match="G07_LIVE_SPEC_MIGRATION_BINDING_INVALID"):
        validate_g07_live_provider_spec(_rehash(spec))


def test_g07_live_spec_output_is_write_once(tmp_path: Path) -> None:
    _built(tmp_path)
    with pytest.raises(P6ContractError, match="G07_LIVE_SPEC_OUTPUT_NOT_EMPTY"):
        build_g07_live_provider_spec(
            output_root=tmp_path / "inputs",
            repo_root=REPOSITORY_ROOT,
            formal=False,
            subject_commit=SUBJECT,
            candidate_tree=TREE,
        )


def test_g07_g4_runner_accepts_redacted_18_call_matrix(tmp_path: Path) -> None:
    _spec, spec_path = _built(tmp_path)
    receipt = asyncio.run(
        run_g07_live_provider_gate(
            candidate_run_spec_path=spec_path,
            output_root=tmp_path / "g4",
            repo_root=REPOSITORY_ROOT,
            formal=False,
            settings=_settings(),
            live_runner=_fake_live_runner(),
        )
    )
    assert receipt["status"] == "PASS"
    assert receipt["metrics"]["network_call_count"] == 18
    assert receipt["metrics"]["fixture_fallback_count"] == 0
    assert receipt["claim_boundary"].endswith("QWEN_IS_SEPARATE")


def test_g07_g4_runner_rejects_fixture_fallback(tmp_path: Path) -> None:
    _spec, spec_path = _built(tmp_path)
    with pytest.raises(P6ContractError, match="P6_G4_PROVIDER_RECEIPTS_INVALID"):
        asyncio.run(
            run_g07_live_provider_gate(
                candidate_run_spec_path=spec_path,
                output_root=tmp_path / "g4",
                repo_root=REPOSITORY_ROOT,
                formal=False,
                settings=_settings(),
                live_runner=_fake_live_runner(fallback=True),
            )
        )
