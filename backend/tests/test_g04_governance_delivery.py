from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest
import yaml

from governance.core_mainline import (
    CONTRACT_PATH,
    CoreMainlineError,
    product_fingerprint,
    validate_delivery_receipt,
)
from governance.g04_screenshot_parity import (
    FORMAL_EVIDENCE_KEY,
    FORMAL_EVIDENCE_LEVEL,
    FORMAL_EXECUTION_MODE,
    FORMAL_RECEIPT_PATH,
    FORMAL_SCHEMA_VERSION,
    G04ParityReceiptError,
    canonical_receipt_hash,
    validate_g04_delivery_evidence,
)
from governance.work_packages_v3 import validate_registry_v3
from scripts.ci_posix_cmd_junction_shim import main as create_posix_junction
from scripts.export_trip_check_openapi import _normalize_schema


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
HEX_A = "a" * 64
HEX_B = "b" * 64


def test_openapi_export_normalizes_singleton_literals_across_pydantic_versions() -> None:
    literal = {"const": "FULL", "enum": ["FULL"], "type": "string"}
    component = {"const": "WRONG_CITY", "enum": ["WRONG_CITY"], "type": "string"}

    assert _normalize_schema(literal, path=("properties", "mode")) == {
        "const": "FULL",
        "type": "string",
    }
    assert _normalize_schema(
        component,
        path=("components", "schemas", "ResolutionRejectionReason"),
    ) == {
        "enum": ["WRONG_CITY"],
        "type": "string",
    }


def test_posix_cmd_shim_only_creates_the_legacy_junction_equivalent(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "junction"

    assert create_posix_junction(["/c", "mklink", "/J", str(link), str(target)]) == 0
    assert link.is_symlink()
    assert link.resolve() == target.resolve()
    assert create_posix_junction(["/c", "echo", "unsafe", str(link), str(target)]) == 2


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout.strip()


def _candidate_repo(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "repo"
    product = root / "backend/app/main.py"
    product.parent.mkdir(parents=True)
    product.write_text("SCREENSHOT_PARITY = 'candidate'\n", encoding="utf-8")
    for relative in (
        "backend/eval_data/g04_screenshot/licensed_baseline_v1.json",
        "backend/scripts/run_g04_paddle_gate.py",
        "backend/evals/g04_screenshot/scorer.py",
    ):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"candidate artifact: {relative}\n", encoding="utf-8")
    _git(root, "init", "-b", "develop")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "G04 Governance Test")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "candidate product")
    return root, _git(root, "rev-parse", "HEAD")


def _formal_receipt(root: Path, candidate_commit: str) -> dict[str, object]:
    fingerprint = product_fingerprint(root)

    def candidate_sha256(relative: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(root), "show", f"{candidate_commit}:{relative}"],
            check=True,
            capture_output=True,
        )
        return hashlib.sha256(result.stdout).hexdigest()

    receipt: dict[str, object] = {
        "schema_version": FORMAL_SCHEMA_VERSION,
        "goal_id": "TC-VNEXT-G04-SCREENSHOT",
        "evidence_level": FORMAL_EVIDENCE_LEVEL,
        "execution_mode": FORMAL_EXECUTION_MODE,
        "sanitized": True,
        "candidate": {
            "commit": candidate_commit,
            "tree": _git(root, "rev-parse", f"{candidate_commit}^{{tree}}"),
            "product_fingerprint": fingerprint,
        },
        "evaluator": {
            "baseline_manifest_sha256": candidate_sha256(
                "backend/eval_data/g04_screenshot/licensed_baseline_v1.json"
            ),
            "expected_transcript_sha256": HEX_B,
            "runspec_sha256": HEX_A,
            "runner_sha256": candidate_sha256(
                "backend/scripts/run_g04_paddle_gate.py"
            ),
            "scorer_sha256": candidate_sha256(
                "backend/evals/g04_screenshot/scorer.py"
            ),
            "text_only_day_dataset_sha256": HEX_B,
            "oracle_adjudication_sha256": HEX_A,
            "metric_inputs_sha256": HEX_B,
            "scored_outputs_sha256": HEX_A,
        },
        "source_policy": {
            "originals_in_git": False,
            "originals_storage": "OUTSIDE_GIT_EPHEMERAL",
            "license_manifest_sha256": HEX_A,
            "cleanup_receipts_sha256": HEX_B,
            "terminal_cleanup_receipts_complete": True,
            "parity_metric_scope": "LICENSED_REAL_ONLY",
            "review_label": "MULTI_AGENT_SIMULATED_REVIEW",
            "human_review": False,
            "candidate_outputs_used_for_oracle": False,
        },
        "paddle": {
            "paddleocr_version": "3.7.0",
            "paddlepaddle_version": "3.3.1",
            "model_sha256": HEX_A,
            "config_sha256": HEX_B,
        },
        "hardware": {
            "device_class": "LOCAL_GPU",
            "hardware_sha256": HEX_A,
            "driver_sha256": HEX_B,
        },
        "performance": {
            "image_count": 3,
            "width_px": 1080,
            "height_px": 1920,
            "concurrency": 1,
            "warmup_runs": 2,
            "measured_runs": 20,
            "measurement_ms": list(range(1000, 1020)),
            "p95_ms": 1018,
        },
        "metric_counts": {
            "case_count": 3,
            "licensed_real_case_count": 2,
            "synthetic_case_count": 1,
            "text_only_day_case_count": 2,
            "critical_field_count": 10,
            "low_confidence_critical_field_count": 2,
            "reading_adjacency_count": 8,
            "location_baseline_count": 5,
            "cleanup_terminal_count": 3,
            "cleanup_receipt_count": 3,
        },
        "metrics": {
            "critical_field_f1": 0.96,
            "low_confidence_confirmation_recall": 1.0,
            "reading_order_adjacency_f1": 0.98,
            "location_precision_drop_pp": 0.5,
            "location_recall_drop_pp": 0.5,
            "wrong_city_count": 0,
            "wrong_category_count": 0,
            "sentence_as_place_count": 0,
            "internal_leak_count": 0,
            "cleanup_receipt_coverage": 1.0,
        },
        "decision": {"status": "PASS", "failures": []},
        "receipt_hash": "",
    }
    receipt["receipt_hash"] = canonical_receipt_hash(receipt)
    return receipt


def _install_formal_receipt(
    root: Path,
    receipt: dict[str, object],
) -> dict[str, str]:
    path = root / FORMAL_RECEIPT_PATH
    _write_json(path, receipt)
    _git(root, "add", FORMAL_RECEIPT_PATH)
    fingerprint = str(receipt["candidate"]["product_fingerprint"])
    return {
        "path": FORMAL_RECEIPT_PATH,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "candidate_commit": str(receipt["candidate"]["commit"]),
        "product_fingerprint": fingerprint,
    }


def test_formal_receipt_binds_candidate_product_paddle_hardware_and_2_plus_20(
    tmp_path: Path,
) -> None:
    root, candidate = _candidate_repo(tmp_path)
    formal = _formal_receipt(root, candidate)
    evidence = _install_formal_receipt(root, formal)
    delivery = {FORMAL_EVIDENCE_KEY: evidence}

    result = validate_g04_delivery_evidence(
        root,
        delivery,
        expected_product_fingerprint=product_fingerprint(root),
        current_product_fingerprint=product_fingerprint(root),
    )

    assert result["decision"] == {"status": "PASS", "failures": []}


@pytest.mark.parametrize(
    ("mutator", "message"),
    [
        (lambda value: value["decision"].update(status="NOT_EVALUABLE"), "not PASS"),
        (
            lambda value: value["metric_counts"].update(
                low_confidence_critical_field_count=0
            ),
            "positive integer",
        ),
        (lambda value: value["performance"].update(warmup_runs=0), r"2\+20"),
        (
            lambda value: value["source_policy"].update(
                candidate_outputs_used_for_oracle=True
            ),
            "own oracle",
        ),
    ],
)
def test_formal_receipt_fails_closed_for_not_evaluable_empty_or_circular_evidence(
    tmp_path: Path,
    mutator: object,
    message: str,
) -> None:
    root, candidate = _candidate_repo(tmp_path)
    formal = _formal_receipt(root, candidate)
    mutator(formal)
    formal["receipt_hash"] = canonical_receipt_hash(formal)
    evidence = _install_formal_receipt(root, formal)

    with pytest.raises(G04ParityReceiptError, match=message):
        validate_g04_delivery_evidence(
            root,
            {FORMAL_EVIDENCE_KEY: evidence},
            expected_product_fingerprint=product_fingerprint(root),
            current_product_fingerprint=product_fingerprint(root),
        )


def test_formal_receipt_becomes_stale_after_product_bytes_change(tmp_path: Path) -> None:
    root, candidate = _candidate_repo(tmp_path)
    formal = _formal_receipt(root, candidate)
    evidence = _install_formal_receipt(root, formal)
    (root / "backend/app/main.py").write_text(
        "SCREENSHOT_PARITY = 'different product'\n",
        encoding="utf-8",
    )

    with pytest.raises(G04ParityReceiptError, match="stale for current product bytes"):
        validate_g04_delivery_evidence(
            root,
            {FORMAL_EVIDENCE_KEY: evidence},
            expected_product_fingerprint=evidence["product_fingerprint"],
            current_product_fingerprint=product_fingerprint(root),
        )


def test_product_delivery_pass_cannot_omit_formal_g04_receipt(tmp_path: Path) -> None:
    root, _candidate = _candidate_repo(tmp_path)
    contract = json.loads((REPOSITORY_ROOT / CONTRACT_PATH).read_text(encoding="utf-8"))
    _write_json(root / CONTRACT_PATH, contract)
    _write_json(
        root / "docs/governance/current_goal_binding.json",
        {"goal_sequence": 4},
    )
    goal = contract["goals"][3]
    _write_json(
        root / "docs/governance/gate-results/G04.product-delivery.json",
        {
            "schema_version": "product-delivery-result-v1",
            "goal_id": goal["goal_id"],
            "gate_profile": goal["gate_profile"],
            "product_fingerprint": product_fingerprint(root),
            "checks": {name: "PASS" for name in goal["required_checks"]},
            "verdict": "PASS",
        },
    )

    with pytest.raises(CoreMainlineError, match="formal parity evidence"):
        validate_delivery_receipt(root, 4)


def test_sequence_four_has_explicit_fixture_and_historical_jobs_not_a_real_paddle_run() -> None:
    workflow_path = REPOSITORY_ROOT / ".github/workflows/core-mainline.yml"
    workflow = workflow_path.read_text(
        encoding="utf-8"
    )
    workflow_contract = yaml.safe_load(workflow)
    jobs = workflow_contract["jobs"]
    g04_jobs = (
        "g04_screenshot_targeted",
        "g04_postgresql",
        "g04_historical_backend",
        "frontend_build",
        "g04_browser_e2e",
    )
    for job_name in g04_jobs:
        assert f"\n  {job_name}:\n    name: {job_name}\n" in workflow
        assert jobs[job_name]["needs"] == "core-mainline-preflight"
    assert jobs["core-mainline-preflight"]["name"] == "core-mainline-preflight"
    aggregator = jobs["core-mainline"]
    assert aggregator["name"] == "core-mainline"
    assert aggregator["if"] == "${{ always() }}"
    assert set(aggregator["needs"]) == {"core-mainline-preflight", *g04_jobs}
    assert sum(job.get("name") == "core-mainline" for job in jobs.values()) == 1
    enforcement = aggregator["steps"][0]["run"]
    assert 'PREFLIGHT_RESULT" != "success' in enforcement
    assert 'expected_g04_result="success"' in enforcement
    assert 'expected_g04_result="skipped"' in enforcement
    assert 'actual_result" != "$expected_g04_result' in enforcement
    assert workflow.count("G04_EVIDENCE_LEVEL: AUTOMATED_FIXTURE_CI") >= 3
    assert "run_g04_screenshot_parity" not in workflow
    assert workflow.count(
        "ref: ${{ github.event.pull_request.head.sha || github.sha }}"
    ) == 6
    preflight_steps = jobs["core-mainline-preflight"]["steps"]
    scope_step = next(
        step for step in preflight_steps if step.get("name") == "Enforce product-mainline scope"
    )
    assert scope_step["env"]["HEAD_SHA"] == "${{ github.event.pull_request.head.sha || github.sha }}"
    assert '--head-ref "$head_ref"' in scope_step["run"]
    postgres_steps = jobs["g04_postgresql"]["steps"]
    compatibility_step = next(
        step
        for step in postgres_steps
        if step.get("name") == "Prepare POSIX backend regression compatibility"
    )
    assert "fonts-noto-cjk" in compatibility_step["run"]
    assert "ci_posix_cmd_junction_shim.py" in compatibility_step["run"]
    assert "pydantic==2.10.4" in compatibility_step["run"]
    posix_regression = next(
        step
        for step in postgres_steps
        if step.get("name") == "Verify backend service and non-P5 regression"
    )
    assert "--ignore-glob='tests/test_trip_check_p5*.py'" in posix_regression["run"]
    assert posix_regression["env"] == {
        "P3_OCR_FONT_PATH": "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
    }
    historical_job = jobs["g04_historical_backend"]
    assert historical_job["runs-on"] == "windows-2022"
    assert historical_job["env"] == {
        "P3_OCR_FONT_PATH": "C:\\Windows\\Fonts\\msyh.ttc",
        "P5_OCR_FONT_PATH": "C:\\Windows\\Fonts\\msyh.ttc",
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
        "OMP_NUM_THREADS": "1",
    }
    historical_steps = historical_job["steps"]
    historical_python = next(
        step for step in historical_steps if step.get("uses") == "actions/setup-python@v5"
    )
    assert historical_python["with"]["python-version"] == "3.13.9"
    assert historical_python["with"]["cache-dependency-path"] == (
        "backend/requirements-historical-p5-ci.txt"
    )
    historical_install = next(
        step
        for step in historical_steps
        if step.get("name") == "Install frozen historical P5 CI profile"
    )
    assert historical_install["run"] == (
        "python -m pip install -r backend/requirements-historical-p5-ci.txt"
    )
    historical_requirements = (
        REPOSITORY_ROOT / "backend/requirements-historical-p5-ci.txt"
    ).read_text(encoding="utf-8")
    assert "pydantic==2.10.4" in historical_requirements
    assert "Pillow==12.2.0" in historical_requirements
    assert "asyncpg==0.31.0" in historical_requirements
    assert "numpy==2.3.5" in historical_requirements
    assert "ortools==9.15.6755" in historical_requirements
    assert "paddlepaddle" not in historical_requirements.lower()
    assert "paddleocr" not in historical_requirements.lower()
    assert "-r requirements" not in historical_requirements
    historical_regression = next(
        step
        for step in historical_steps
        if step.get("name") == "Verify exact historical font and P5 regression"
    )
    assert "d79c55e68b1131eea0cc1c47be4f572d964f28c682e143db2ad09c1e4cb07a3f" in historical_regression["run"]
    assert "P5_RENDERER_ABI" in historical_regression["run"]
    assert "frozen P5 renderer ABI mismatch" in historical_regression["run"]
    assert "test_trip_check_p5*.py" in historical_regression["run"]
    assert "python -m pytest -q @testFiles" in historical_regression["run"]


def test_current_g04_lifecycle_has_frozen_formal_pass_before_delivery() -> None:
    current_goal = (REPOSITORY_ROOT / "docs/governance/CURRENT_GOAL.md").read_text(
        encoding="utf-8"
    )
    registry = json.loads(
        (REPOSITORY_ROOT / "docs/governance/current_work_packages.json").read_text(
            encoding="utf-8"
        )
    )

    assert current_goal.startswith("# IN_PROGRESS GOAL")
    assert registry["delivery_evidence"]["state"] == "EVIDENCE_FROZEN"
    assert registry["delivery_evidence"]["formal_parity"]["status"] == "PASS"
    assert validate_registry_v3(REPOSITORY_ROOT)["verdict"] == "PASS"
