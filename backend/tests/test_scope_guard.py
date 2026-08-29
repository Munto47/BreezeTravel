from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from evals.agent_gate_v1.contracts import ActiveSlice
from evals.agent_gate_v1.scope_guard import POLICY_HASH_PATHS, scope_policy_digest, validate_mainline_scope


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8", newline="\n")


def _active_slice(
    *,
    base_commit: str,
    work_kind: str,
    phase: str = "IMPLEMENTING",
    frozen_candidate_commit: str | None = None,
    preflight_entrypoints: list[str] | None = None,
    preflight_required_tokens: dict[str, list[str]] | None = None,
) -> dict[str, object]:
    value: dict[str, object] = {
        "slice_id": "G01-SCOPE-GUARD-TEST",
        "base_commit": base_commit,
        "work_kind": work_kind,
        "phase": phase,
        "user_outcome": "The current product mainline stays ahead of governance work.",
        "acceptance_refs": ["CURRENT_GOAL acceptance"],
        "allowed_paths": [
            "AGENTS.md",
            "backend",
            "docs/governance/current_work_packages.json",
            "frontend",
        ],
        "minimum_change": "Make the smallest current-scope change.",
        "forbidden_mechanisms": ["new durable governance state"],
        "evidence_invalidated": [],
        "stop_condition": "Stop when the change requires candidate hardening.",
        "preflight_entrypoints": preflight_entrypoints or [],
        "preflight_required_tokens": preflight_required_tokens or {},
    }
    if work_kind == "CURRENT_GATE_FIX":
        value["blocking_issue"] = {
            "severity": "P1",
            "reproduction": "python -m pytest test_current_gate.py",
            "current_goal_acceptance_ref": "CURRENT_GOAL acceptance",
            "impact_chain": "The current Gate could report a false PASS.",
            "minimum_fix": "Repair only the reproduced Gate defect.",
            "stop_condition": "Stop when the reproduced defect passes.",
        }
    if work_kind == "HARDENING":
        value["hardening_decision_ref"] = "G07 approved hardening decision"
    if frozen_candidate_commit is not None:
        value["frozen_candidate_commit"] = frozen_candidate_commit
    return value


def _registry(
    root: Path,
    *,
    active_slice: dict[str, object],
    goal_sequence: int,
) -> dict[str, object]:
    goal_id = (
        "TC-VNEXT-G07-CANDIDATE"
        if goal_sequence == 7
        else "TC-VNEXT-G01-TEXT-CARDS"
    )
    return {
        "schema_version": "work-package-registry-v2",
        "scope_guard_version": "scope-guard-v1",
        "scope_policy_sha256": scope_policy_digest(root),
        "active_slice": active_slice,
        "program_id": "TC-VNEXT-2026",
        "active_goal_sequence": goal_sequence,
        "active_goal_id": goal_id,
        "mainline_phase": "CANDIDATE_HARDENING" if goal_sequence == 7 else "CORE_MVP",
        "gate_profile": (
            "HARDENED_CANDIDATE_GATE" if goal_sequence == 7 else "CORE_AGENT_GATE"
        ),
        "guidance_sha256": "1" * 64,
        "mismatch_policy": "READ_ONLY",
        "max_parallel_writers": 3,
        "max_prepared_next_goal_packages": 2,
        "integration_order": [],
        "e2e_after_all_merges": True,
        "packages": [
            {
                "package_id": f"WP-G{goal_sequence:02d}-INTEGRATOR",
                "goal_id": goal_id,
                "baseline_commit": "2" * 40,
                "branch": "codex/integrator",
                "remote_branch": "origin/codex/integrator",
                "worktree_path": root.resolve().as_posix(),
                "role": "INTEGRATOR",
                "execution_mode": "PRIMARY_INTEGRATOR_DIALOGUE",
                "dialogue_ref": "codex-task:primary",
                "dependencies": [],
                "owned_paths": ["backend"],
                "forbidden_paths": [],
                "acceptance": ["scope guard succeeds"],
                "merge_order": 1,
                "status": "IN_PROGRESS",
            }
        ],
    }


def _scope_repo(
    tmp_path: Path,
    *,
    work_kind: str,
    goal_sequence: int = 1,
    phase: str = "IMPLEMENTING",
    preflight_entrypoints: list[str] | None = None,
    preflight_required_tokens: dict[str, list[str]] | None = None,
) -> tuple[Path, str]:
    root = tmp_path / "repo"
    root.mkdir(parents=True)
    _git(root, "init")
    _git(root, "config", "user.email", "scope@example.com")
    _git(root, "config", "user.name", "Scope Guard")
    _write(root / "AGENTS.md", "baseline guidance\n")
    for relative in POLICY_HASH_PATHS:
        path = root / relative
        if not path.exists():
            _write(path, f"baseline policy for {relative}\n")
    initial_slice = _active_slice(
        base_commit="0" * 40,
        work_kind=work_kind,
        phase="IMPLEMENTING",
        preflight_entrypoints=preflight_entrypoints,
        preflight_required_tokens=preflight_required_tokens,
    )
    registry_path = root / "docs/governance/current_work_packages.json"
    _write(
        registry_path,
        json.dumps(
            _registry(root, active_slice=initial_slice, goal_sequence=goal_sequence),
            indent=2,
        )
        + "\n",
    )
    _git(root, "add", "--all")
    _git(root, "commit", "-m", "install scope policy")
    base = _git(root, "rev-parse", "HEAD")
    active = _active_slice(
        base_commit=base,
        work_kind=work_kind,
        phase=phase if phase == "PREFLIGHT" else "IMPLEMENTING",
        preflight_entrypoints=preflight_entrypoints,
        preflight_required_tokens=preflight_required_tokens,
    )
    _write(
        registry_path,
        json.dumps(
            _registry(root, active_slice=active, goal_sequence=goal_sequence),
            indent=2,
        )
        + "\n",
    )
    _git(root, "add", "--all")
    _git(root, "commit", "-m", "activate slice")
    if phase in {"EVIDENCE_FROZEN", "GATE_RUNNING"}:
        candidate = _git(root, "rev-parse", "HEAD")
        active = _active_slice(
            base_commit=base,
            work_kind=work_kind,
            phase=phase,
            frozen_candidate_commit=candidate,
            preflight_entrypoints=preflight_entrypoints,
            preflight_required_tokens=preflight_required_tokens,
        )
        _write(
            registry_path,
            json.dumps(
                _registry(root, active_slice=active, goal_sequence=goal_sequence),
                indent=2,
            )
            + "\n",
        )
        _git(root, "add", "--all")
        _git(root, "commit", "-m", "freeze candidate")
    return root, base


def test_product_runtime_slice_passes_and_derives_progress(tmp_path: Path) -> None:
    root, _base = _scope_repo(tmp_path, work_kind="PRODUCT")
    _write(root / "backend/app/service.py", "VALUE = 'user-visible'\n")

    report = validate_mainline_scope(root)

    assert report.verdict == "PASS"
    assert report.product_progress == ["RUNTIME"]


def test_eval_infrastructure_never_claims_product_progress(tmp_path: Path) -> None:
    root, _base = _scope_repo(tmp_path, work_kind="EVAL_INFRA")
    _write(root / "backend/evals/helper.py", "VALUE = 'contract-only'\n")

    report = validate_mainline_scope(root)

    assert report.verdict == "PASS"
    assert report.product_progress == ["NONE"]


def test_work_kind_cannot_hide_eval_or_product_scope(tmp_path: Path) -> None:
    root, _base = _scope_repo(tmp_path, work_kind="PRODUCT")
    _write(root / "backend/evals/helper.py", "VALUE = 'not product progress'\n")
    report = validate_mainline_scope(root)
    assert report.verdict == "REJECT"
    assert "product_slice_without_product_progress" in report.detected_mechanisms

    root, _base = _scope_repo(tmp_path / "second", work_kind="EVAL_INFRA")
    _write(root / "backend/app/service.py", "VALUE = 'runtime change'\n")
    report = validate_mainline_scope(root)
    assert report.verdict == "REJECT"
    assert "eval_infra_changed_product_runtime" in report.detected_mechanisms


def test_g01_custody_or_crypto_is_deferred_to_g07(tmp_path: Path) -> None:
    root, _base = _scope_repo(tmp_path, work_kind="CURRENT_GATE_FIX")
    _write(
        root / "backend/evals/custody_registry.py",
        "import hmac\nimport sqlite3\nSQL = 'CREATE TABLE sealed_runs (id int)'\n",
    )

    report = validate_mainline_scope(root)

    assert report.verdict == "DEFER_TO_G07"
    assert "NEW_DURABLE_EVAL_STATE" in report.error_codes
    assert "NEW_CRYPTO_PROTOCOL" in report.error_codes


def test_same_hardening_mechanism_is_allowed_in_g07(tmp_path: Path) -> None:
    root, _base = _scope_repo(
        tmp_path,
        work_kind="HARDENING",
        goal_sequence=7,
    )
    _write(
        root / "backend/evals/custody_registry.py",
        "import sqlite3\nSQL = 'CREATE TABLE sealed_runs (id int)'\n",
    )

    report = validate_mainline_scope(root)

    assert report.verdict == "PASS"
    assert "NEW_DURABLE_EVAL_STATE" not in report.error_codes


def test_policy_self_modification_is_rejected(tmp_path: Path) -> None:
    root, _base = _scope_repo(tmp_path, work_kind="PRODUCT")
    _write(root / "AGENTS.md", "weakened policy\n")

    report = validate_mainline_scope(root)

    assert report.verdict == "REJECT"
    assert "POLICY_SELF_MODIFICATION" in report.error_codes


@pytest.mark.parametrize("case", ["files", "lines", "schemas"])
def test_eval_infrastructure_budget_requires_review(tmp_path: Path, case: str) -> None:
    root, _base = _scope_repo(tmp_path, work_kind="EVAL_INFRA")
    if case == "files":
        for index in range(6):
            _write(root / f"backend/evals/part_{index}.py", "VALUE = 1\n")
    elif case == "lines":
        _write(
            root / "backend/evals/large_fix.py",
            "\n".join(f"VALUE_{index} = {index}" for index in range(301)) + "\n",
        )
    else:
        for index in range(3):
            _write(root / f"backend/eval_data/new_{index}.schema.json", "{}\n")

    report = validate_mainline_scope(root)

    assert report.verdict == "SCOPE_REVIEW_REQUIRED"
    assert "BUDGET_EXCEEDED" in report.error_codes


def test_gate_fix_requires_reproduction_and_current_goal_impact() -> None:
    with pytest.raises(ValueError, match="reproducible blocking issue"):
        ActiveSlice.model_validate(
            _active_slice(base_commit="1" * 40, work_kind="EVAL_INFRA")
            | {"work_kind": "CURRENT_GATE_FIX"}
        )


def test_hardening_requires_an_explicit_g07_decision() -> None:
    value = _active_slice(base_commit="1" * 40, work_kind="EVAL_INFRA")
    with pytest.raises(ValueError, match="explicit G07 decision"):
        ActiveSlice.model_validate(value | {"work_kind": "HARDENING"})


def test_preflight_rejects_unconsumed_cli_and_true_pass_default(tmp_path: Path) -> None:
    entrypoint = "backend/scripts/gate_entry.py"
    root, _base = _scope_repo(
        tmp_path,
        work_kind="EVAL_INFRA",
        phase="PREFLIGHT",
        preflight_entrypoints=[entrypoint],
    )
    _write(
        root / entrypoint,
        "import argparse\n"
        "verified: bool = True\n"
        "parser = argparse.ArgumentParser()\n"
        "parser.add_argument('--mint')\n"
        "args = parser.parse_args()\n",
    )

    report = validate_mainline_scope(root)

    assert report.verdict == "REJECT"
    assert "STAGE_SCOPE_VIOLATION" in report.error_codes
    assert any(item.startswith("unconsumed_cli_argument") for item in report.detected_mechanisms)
    assert any(item.startswith("unsafe_true_default") for item in report.detected_mechanisms)


def test_preflight_requires_exclusive_output_and_upstream_readback(tmp_path: Path) -> None:
    entrypoint = "backend/scripts/gate_entry.py"
    root, _base = _scope_repo(
        tmp_path,
        work_kind="EVAL_INFRA",
        phase="PREFLIGHT",
        preflight_entrypoints=[entrypoint],
        preflight_required_tokens={
            entrypoint: ["output.open(\"x\"", "read_upstream_receipt"],
        },
    )
    _write(root / entrypoint, "RESULT = 'does not read its producer'\n")

    report = validate_mainline_scope(root)

    assert report.verdict == "REJECT"
    assert sum(
        item.startswith("missing_required_readback")
        for item in report.detected_mechanisms
    ) == 2


def test_tracked_change_after_freeze_is_rejected(tmp_path: Path) -> None:
    root, _base = _scope_repo(
        tmp_path,
        work_kind="EVAL_INFRA",
        phase="EVIDENCE_FROZEN",
    )
    frozen = validate_mainline_scope(root, requested_phase="EVIDENCE_FROZEN")
    assert frozen.verdict == "PASS"
    gate_entry = validate_mainline_scope(root, requested_phase="GATE_RUNNING")
    assert gate_entry.verdict == "PASS"

    _write(root / "backend/app/service.py", "VALUE = 'changed after freeze'\n")

    report = validate_mainline_scope(root, requested_phase="EVIDENCE_FROZEN")

    assert report.verdict == "REJECT"
    assert "EVIDENCE_FREEZE_BROKEN" in report.error_codes


def test_large_custody_subsystem_cannot_pass_g01(tmp_path: Path) -> None:
    root, _base = _scope_repo(tmp_path, work_kind="EVAL_INFRA")
    body = ["import sqlite3", "SQL = 'CREATE TABLE sealed_runs (id int)'"]
    body.extend(f"VALUE_{index} = {index}" for index in range(2100))
    _write(root / "backend/evals/core_sealed_custody.py", "\n".join(body) + "\n")

    report = validate_mainline_scope(root)

    assert report.verdict == "DEFER_TO_G07"
    assert "NEW_DURABLE_EVAL_STATE" in report.error_codes
    assert "BUDGET_EXCEEDED" in report.error_codes
