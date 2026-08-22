from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from evals.continuous import preflight, run_foundation
from evals.continuous.__main__ import main


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout.strip()


def _case(*, split: str = "dev", provider_mode: str = "controlled_fixture") -> dict:
    return {
        "schema_version": "dual-entry-case-v1",
        "case_id": f"{split}.bj.import.01",
        "split": split,
        "entry": "IMPORT",
        "city": "北京",
        "trip_days": 2,
        "group_size": 2,
        "source_family_id": f"{split}-family-01",
        "data_origin": "controlled_mutation",
        "source_document_refs": ["source-01"],
        "input": {"raw_itinerary": "D1 故宫；D2 颐和园"},
        "execution": {
            "provider_mode": provider_mode,
            "fault_profile": None,
            "steps": ["create_workspace", "import_text", "apply_import", "readback"],
        },
    }


def _spec(*, split: str = "dev", lane: str = "pr_offline", provider_mode: str = "controlled_fixture") -> dict:
    snapshot = provider_mode == "frozen_snapshot"
    return {
        "schema_version": "dual-entry-run-spec-v1",
        "lane": lane,
        "purpose": "regression",
        "sut": {
            "commit_sha": "RESOLVE_AT_RUN_START",
            "dirty_diff_sha256": "RESOLVE_AT_RUN_START",
            "runtime_config_sha256": "RESOLVE_AT_RUN_START",
            "docker_image_digest": "RESOLVE_AT_RUN_START",
            "base_url": "http://127.0.0.1:8000",
            "allow_direct_domain_calls": False,
            "allow_sql_seed": False,
        },
        "dataset": {
            "manifest": "backend/eval_data/dual_entry_v1/manifest.json",
            "manifest_sha256": "RESOLVE_AT_RUN_START",
            "case_ids_sha256": "RESOLVE_AT_RUN_START",
            "splits": [split],
            "hash_policy": "compute_and_freeze_before_run",
            "label_access": "development_scorer",
        },
        "provider": {
            "mode": provider_mode,
            "fixture_fallback_allowed": provider_mode == "controlled_fixture",
            "receipts_required": True,
            "snapshot_id": "snapshot-01" if snapshot else None,
            "snapshot_sha256": "b" * 64 if snapshot else None,
            "snapshot_path": None,
        },
        "models": {
            "generator": {"enabled": False, "model": "disabled", "prompt_sha256": "disabled"},
            "parser_fallback": {"enabled": False, "model": "disabled", "prompt_sha256": "disabled"},
            "correction": {"enabled": False, "model": "disabled", "prompt_sha256": "disabled"},
            "judge": {
                "enabled": False,
                "model": "disabled",
                "prompt_sha256": "disabled",
                "rubric_version": "semantic-rubric-v1",
            },
        },
        "comparison": {
            "baseline_run_id": "OPTIONAL_FOR_PR",
            "threshold_set_version": "dual-entry-gates-v1",
            "paired_only": True,
            "confidence": 0.95,
            "noninferiority_margin": 0.02,
        },
        "execution": {
            "resume_at_case_boundary": True,
            "normalize_volatile_fields": True,
            "cache_namespace_sha256": "RESOLVE_AT_RUN_START",
            "retry_policy_version": "test-v1",
        },
        "budget": {
            "paid_api_allowed": False,
            "max_total_cost_cny": 0,
            "max_concurrency": 1,
            "max_case_seconds": 30,
        },
        "thresholds": {"contract_pass_rate": 1.0},
        "artifacts": ["run_spec.json", "product_outputs.jsonl", "provider_receipts.jsonl", "cost.json", "gate.json"],
        "prohibitions": ["paid_api_call", "sql_seed", "blind_label_access", "fixture_marked_live"],
    }


@pytest.fixture
def continuous_repo(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    data_root = repo / "backend" / "eval_data" / "dual_entry_v1"
    spec_path = repo / "backend" / "evals" / "run_specs" / "spec.json"
    case = _case()
    _write_jsonl(data_root / "dev.inputs.jsonl", [case])
    raw_archive = data_root / "archives" / "source-01" / "raw.json"
    extract_archive = data_root / "archives" / "source-01" / "extract.json"
    raw_archive.parent.mkdir(parents=True, exist_ok=True)
    raw_archive.write_bytes(b"raw source fixture\n")
    extract_archive.write_bytes(b"structured source fixture\n")
    raw_hash = hashlib.sha256(raw_archive.read_bytes()).hexdigest()
    extract_hash = hashlib.sha256(extract_archive.read_bytes()).hexdigest()
    _write_jsonl(
        data_root / "source_registry.jsonl",
        [
            {
                "source_document_id": "source-01",
                "canonical_url": "https://example.test/route",
                "access_status": "VERIFIED_ACCESSIBLE",
                "raw_hash": raw_hash,
                "extract_hash": extract_hash,
                "raw_archive_path": "archives/source-01/raw.json",
                "extract_archive_path": "archives/source-01/extract.json",
            }
        ],
    )
    _write_json(
        data_root / "manifest.json",
        {
            "schema_version": "dual-entry-manifest-v1",
            "dataset_id": "test-dual-entry-v1",
            "files": [{"split": "dev", "inputs": "dev.inputs.jsonl", "labels": "dev.labels.jsonl", "case_count": 1}],
            "source_registry": "source_registry.jsonl",
        },
    )
    _write_json(spec_path, _spec())
    _git(repo.parent, "init", str(repo))
    _git(repo, "add", ".")
    _git(repo, "-c", "user.name=Codex Test", "-c", "user.email=codex@example.test", "commit", "-m", "fixture")
    return repo, spec_path


def _error_codes(result) -> set[str]:
    return {item["code"] for item in result.errors}


def _snapshot_artifact() -> dict:
    artifact = {
        "schema_version": "1.0",
        "evidence_class": "real_provider_local_authorized",
        "evidence_subtype": "suggestion_live_candidate_and_walking_route_snapshot",
        "claim_boundary": {
            "proves_opening_hours": False,
            "is_public_internet_e2e": False,
            "is_human_evidence": False,
            "is_release_approval": False,
        },
        "overall_status": "passed",
        "cities": [{"city": "北京"}, {"city": "上海"}, {"city": "杭州"}],
    }
    payload_hash = hashlib.sha256(
        json.dumps(artifact, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    artifact["integrity"] = {
        "artifact_payload_sha256": payload_hash,
        "hash_algorithm": "SHA-256 over canonical UTF-8 JSON excluding integrity",
        "passed": True,
        "validation_errors": [],
    }
    return artifact


def _configure_frozen_snapshot(repo: Path, spec_path: Path) -> tuple[Path, str, str]:
    data_root = repo / "backend" / "eval_data" / "dual_entry_v1"
    case = _case(provider_mode="frozen_snapshot")
    _write_jsonl(data_root / "dev.inputs.jsonl", [case])
    artifact = _snapshot_artifact()
    snapshot_path = repo / "backend" / "evidence" / "snapshots" / "suggestions.json"
    _write_json(snapshot_path, artifact)
    file_hash = hashlib.sha256(snapshot_path.read_bytes()).hexdigest()
    payload_hash = artifact["integrity"]["artifact_payload_sha256"]
    spec = _spec(lane="nightly_snapshot", provider_mode="frozen_snapshot")
    spec["provider"].update({
        "snapshot_id": payload_hash,
        "snapshot_sha256": file_hash,
        "snapshot_path": "backend/evidence/snapshots/suggestions.json",
    })
    _write_json(spec_path, spec)
    return snapshot_path, payload_hash, file_hash


def test_preflight_resolves_and_binds_git_config_dataset_and_sources(continuous_repo):
    repo, spec_path = continuous_repo

    result = preflight(spec_path, repo_root=repo, environ={})

    assert result.valid is True
    assert result.resolved_spec is not None
    assert result.resolved_spec["sut"]["commit_sha"] == _git(repo, "rev-parse", "HEAD")
    assert len(result.resolved_spec["sut"]["dirty_diff_sha256"]) == 64
    assert len(result.resolved_spec["sut"]["runtime_config_sha256"]) == 64
    assert result.resolved_spec["sut"]["docker_image_digest"].startswith("local-worktree@sha256:")
    assert len(result.resolved_spec["dataset"]["manifest_sha256"]) == 64
    assert len(result.resolved_spec["dataset"]["case_ids_sha256"]) == 64
    bindings = result.resolved_spec["execution"]["bindings"]
    assert bindings["selected_case_count"] == 1
    assert len(bindings["dataset_content_sha256"]) == 64
    assert len(bindings["source_registry_sha256"]) == 64
    source = json.loads((repo / "backend/eval_data/dual_entry_v1/source_registry.jsonl").read_text(encoding="utf-8"))
    assert bindings["referenced_source_hashes"]["source-01"] == {
        "raw_hash": source["raw_hash"],
        "extract_hash": source["extract_hash"],
    }
    assert all(check["status"] == "PASS" for check in result.checks)


def test_frozen_snapshot_preflight_binds_exact_file_and_artifact_identity(continuous_repo):
    repo, spec_path = continuous_repo
    snapshot_path, payload_hash, file_hash = _configure_frozen_snapshot(repo, spec_path)

    result = preflight(spec_path, repo_root=repo, environ={})

    assert result.valid is True
    assert result.bindings["provider_snapshot_path"] == "backend/evidence/snapshots/suggestions.json"
    assert result.bindings["provider_snapshot_file_sha256"] == file_hash
    assert result.bindings["provider_snapshot_payload_sha256"] == payload_hash
    assert result.bindings["provider_snapshot_id"] == payload_hash
    assert result.bindings["provider_snapshot_overall_status"] == "passed"
    assert snapshot_path.is_file()
    assert next(
        check for check in result.checks if check["id"] == "PROVIDER_SNAPSHOT_BINDING"
    )["status"] == "PASS"


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ("escape", "SNAPSHOT_PATH_OUTSIDE_REPOSITORY"),
        ("missing", "SNAPSHOT_ARTIFACT_MISSING"),
        ("hash", "SNAPSHOT_ARTIFACT_SHA256_MISMATCH"),
    ],
)
def test_frozen_snapshot_preflight_rejects_unsafe_or_unbound_files(
    continuous_repo,
    mutation,
    expected_code,
):
    repo, spec_path = continuous_repo
    _configure_frozen_snapshot(repo, spec_path)
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    if mutation == "escape":
        spec["provider"]["snapshot_path"] = "../outside.json"
    elif mutation == "missing":
        spec["provider"]["snapshot_path"] = "backend/evidence/snapshots/missing.json"
    else:
        spec["provider"]["snapshot_sha256"] = "0" * 64
    _write_json(spec_path, spec)

    result = preflight(spec_path, repo_root=repo, environ={})

    assert result.valid is False
    assert expected_code in _error_codes(result)


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ("class", "SNAPSHOT_EVIDENCE_CLASS_INVALID"),
        ("subtype", "SNAPSHOT_EVIDENCE_SUBTYPE_INVALID"),
        ("status", "SNAPSHOT_OVERALL_STATUS_NOT_PASSED"),
        ("integrity", "SNAPSHOT_PAYLOAD_INTEGRITY_MISMATCH"),
        ("id", "SNAPSHOT_ID_NOT_ARTIFACT_BOUND"),
    ],
)
def test_frozen_snapshot_preflight_rejects_invalid_evidence_contract(
    continuous_repo,
    mutation,
    expected_code,
):
    repo, spec_path = continuous_repo
    snapshot_path, _, _ = _configure_frozen_snapshot(repo, spec_path)
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    artifact = json.loads(snapshot_path.read_text(encoding="utf-8"))
    if mutation == "class":
        artifact["evidence_class"] = "controlled_fixture"
    elif mutation == "subtype":
        artifact["evidence_subtype"] = "unrelated_snapshot"
    elif mutation == "status":
        artifact["overall_status"] = "failed"
    elif mutation == "integrity":
        artifact["cities"][0]["city"] = "伪造城市"
    else:
        spec["provider"]["snapshot_id"] = "0" * 64
    if mutation != "id":
        _write_json(snapshot_path, artifact)
        spec["provider"]["snapshot_sha256"] = hashlib.sha256(snapshot_path.read_bytes()).hexdigest()
    _write_json(spec_path, spec)

    result = preflight(spec_path, repo_root=repo, environ={})

    assert result.valid is False
    assert expected_code in _error_codes(result)


def test_dirty_diff_and_dataset_binding_change_when_input_changes(continuous_repo):
    repo, spec_path = continuous_repo
    clean = preflight(spec_path, repo_root=repo, environ={})
    input_path = repo / "backend" / "eval_data" / "dual_entry_v1" / "dev.inputs.jsonl"
    changed_case = _case()
    changed_case["input"]["raw_itinerary"] = "D1 天坛；D2 故宫"
    _write_jsonl(input_path, [changed_case])

    changed = preflight(spec_path, repo_root=repo, environ={})

    assert clean.valid and changed.valid
    assert clean.bindings["dirty_diff_sha256"] != changed.bindings["dirty_diff_sha256"]
    assert clean.bindings["dataset_content_sha256"] != changed.bindings["dataset_content_sha256"]


def test_preflight_rejects_referenced_source_archive_hash_mismatch(continuous_repo):
    repo, spec_path = continuous_repo
    archive_path = repo / "backend/eval_data/dual_entry_v1/archives/source-01/raw.json"
    archive_path.write_bytes(b"tampered source fixture\n")

    result = preflight(spec_path, repo_root=repo, environ={})

    assert result.valid is False
    assert "REFERENCED_SOURCE_RAW_ARCHIVE_HASH_MISMATCH" in _error_codes(result)


def test_preflight_can_bind_an_explicit_case_subset_without_mixing_provider_modes(continuous_repo):
    repo, spec_path = continuous_repo
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    spec["dataset"]["case_ids"] = ["dev.bj.import.01"]
    _write_json(spec_path, spec)

    result = preflight(spec_path, repo_root=repo, environ={})

    assert result.valid is True
    assert result.bindings["selected_case_count"] == 1


def test_required_placeholder_is_rejected_before_execution(continuous_repo):
    repo, spec_path = continuous_repo
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    spec["comparison"]["baseline_run_id"] = "REQUIRED_AT_RUN_START"
    _write_json(spec_path, spec)

    result = preflight(spec_path, repo_root=repo, environ={})

    assert result.valid is False
    assert "UNRESOLVED_REQUIRED_PLACEHOLDER" in _error_codes(result)


def test_auto_placeholder_is_rejected_outside_runner_owned_bindings(continuous_repo):
    repo, spec_path = continuous_repo
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    spec["comparison"]["baseline_run_id"] = "RESOLVE_AT_RUN_START"
    _write_json(spec_path, spec)

    result = preflight(spec_path, repo_root=repo, environ={})

    assert result.valid is False
    assert "UNRESOLVED_AUTO_PLACEHOLDER" in _error_codes(result)


def test_blind_label_path_and_oracle_in_input_are_rejected(continuous_repo):
    repo, spec_path = continuous_repo
    data_root = repo / "backend" / "eval_data" / "dual_entry_v1"
    blind_case = _case(split="frozen_blind", provider_mode="frozen_snapshot")
    blind_case["deterministic_truth"] = {"must_pass": ["SECRET_EXPECTATION"]}
    _write_jsonl(data_root / "frozen_blind.inputs.jsonl", [blind_case])
    manifest = json.loads((data_root / "manifest.json").read_text(encoding="utf-8"))
    manifest["files"] = [
        {
            "split": "frozen_blind",
            "inputs": "frozen_blind.inputs.jsonl",
            "labels": "sealed/frozen_blind.labels.jsonl",
            "case_count": 1,
        }
    ]
    _write_json(data_root / "manifest.json", manifest)
    spec = _spec(split="frozen_blind", lane="release_blind", provider_mode="frozen_snapshot")
    spec["purpose"] = "promotion"
    spec["dataset"]["label_access"] = "isolated_scorer_only"
    spec["models"]["judge"] = {
        "enabled": True,
        "provider": "openai",
        "model": "gpt-5.6-sol",
        "prompt_sha256": "d" * 64,
        "hidden_labels_allowed": False,
        "label_path": "sealed/frozen_blind.labels.jsonl",
    }
    spec["prohibitions"] += ["generator_access_to_labels", "sut_access_to_labels", "judge_access_to_labels"]
    _write_json(spec_path, spec)

    result = preflight(spec_path, repo_root=repo, environ={})

    assert result.valid is False
    assert {
        "BLIND_LABEL_PATH_EXPOSED",
        "BLIND_ORACLE_IN_PRODUCT_INPUT",
        "REPOSITORY_BLIND_LABEL_PAYLOAD_EXPOSED",
    } <= _error_codes(result)


def test_sql_seed_configuration_is_rejected_even_when_allow_flag_is_false(continuous_repo):
    repo, spec_path = continuous_repo
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    spec["execution"]["bootstrap_command"] = "INSERT INTO room_places VALUES (...)"
    _write_json(spec_path, spec)

    result = preflight(spec_path, repo_root=repo, environ={})

    assert result.valid is False
    assert "SQL_SEED_EXECUTION_CONFIGURED" in _error_codes(result)


def test_live_lane_rejects_mock_runtime_and_fixture_cases(continuous_repo):
    repo, spec_path = continuous_repo
    spec = _spec(lane="weekly_live", provider_mode="live_provider")
    spec["provider"]["fixture_fallback_allowed"] = False
    _write_json(spec_path, spec)

    result = preflight(spec_path, repo_root=repo, environ={"AMAP_MOCK": "true", "DEMO_MODE": "false"})

    assert result.valid is False
    assert {"LIVE_RUNTIME_IS_MOCKED", "CASE_PROVIDER_MODE_MISMATCH"} <= _error_codes(result)


def test_run_writes_only_atomic_foundation_receipts_and_rejects_missing_product_chain(continuous_repo):
    repo, spec_path = continuous_repo

    result = run_foundation(spec_path, repo_root=repo, environ={})

    assert result.gate["status"] == "INVALID"
    assert result.gate["decision"] == "REJECT"
    assert result.gate["phase"] == "EXECUTION_NOT_AVAILABLE"
    assert result.gate["execution"] == {
        "attempted": False,
        "product_http_calls": 0,
        "adapter": None,
        "stages": [],
        "reason": "PRODUCT_ADAPTER_AND_STAGES_NOT_IMPLEMENTED",
    }
    assert result.gate["gates"][-1] == {
        "id": "PRODUCT_EXECUTION",
        "status": "FAIL",
        "reason": "PRODUCT_ADAPTER_AND_STAGES_NOT_IMPLEMENTED",
    }
    assert {path.name for path in result.run_dir.iterdir()} == {"run_spec.json", "gate.json"}
    assert not list(result.run_dir.glob("*.tmp"))
    artifact_bytes = (result.run_dir / "run_spec.json").read_bytes()
    artifact = json.loads(artifact_bytes)
    assert artifact["run_id"] == result.run_id
    assert datetime.fromisoformat(artifact["started_at"]).tzinfo is not None
    gate = json.loads((result.run_dir / "gate.json").read_text(encoding="utf-8"))
    assert gate["run_spec_artifact_sha256"] == hashlib.sha256(artifact_bytes).hexdigest()
    assert gate["bindings"]["sut_commit"] == _git(repo, "rev-parse", "HEAD")

    replay = run_foundation(spec_path, repo_root=repo, environ={})
    assert replay.gate["bindings"]["dirty_diff_sha256"] == gate["bindings"]["dirty_diff_sha256"]
    assert replay.gate["bindings"]["cache_namespace_sha256"] == gate["bindings"]["cache_namespace_sha256"]


def test_cli_validate_and_run_exit_codes_are_fail_closed(continuous_repo, tmp_path, capsys):
    repo, spec_path = continuous_repo
    assert main(["validate", "--spec", str(spec_path)]) == 0
    validate_output = json.loads(capsys.readouterr().out)
    assert validate_output["status"] == "VALID"

    assert main(["run", "--spec", str(spec_path), "--runs-root", str(tmp_path / "cli-runs")]) == 2
    run_output = json.loads(capsys.readouterr().out)
    assert run_output["decision"] == "REJECT"
    assert run_output["reason"] == "PRODUCT_ADAPTER_AND_STAGES_NOT_IMPLEMENTED"


def test_run_persists_rejected_gate_when_preflight_is_invalid(continuous_repo, tmp_path):
    repo, spec_path = continuous_repo
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    spec["comparison"]["baseline_run_id"] = "REQUIRED_AT_RUN_START"
    _write_json(spec_path, spec)

    result = run_foundation(spec_path, repo_root=repo, runs_root=tmp_path / "invalid-runs", environ={})

    assert result.gate["status"] == "INVALID"
    assert result.gate["decision"] == "REJECT"
    assert result.gate["phase"] == "PREFLIGHT"
    assert result.gate["execution"]["attempted"] is False
    assert result.gate["execution"]["reason"] == "PREFLIGHT_FAILED"
    assert result.gate["gates"][-1] == {
        "id": "PRODUCT_EXECUTION",
        "status": "NOT_RUN",
        "reason": "PREFLIGHT_FAILED",
    }
    assert "UNRESOLVED_REQUIRED_PLACEHOLDER" in {item["code"] for item in result.gate["errors"]}
    assert (result.run_dir / "run_spec.json").is_file()
    assert (result.run_dir / "gate.json").is_file()
