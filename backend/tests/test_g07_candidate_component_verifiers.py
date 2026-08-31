from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from evals.agent_gate_v1.candidate_component_verifiers import (
    CandidateComponentVerificationError,
    VERIFIER_PATH,
    compute_candidate_component_summary,
    verification_summary_sha256,
    verify_candidate_component_receipt,
)
from evals.agent_gate_v1.contracts import CandidateGateComponentReceipt


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _automated_receipt(tmp_path: Path) -> tuple[CandidateGateComponentReceipt, Path]:
    root = tmp_path / "candidate"
    root.mkdir()
    _git(root, "init")
    _git(root, "config", "user.email", "component@example.test")
    _git(root, "config", "user.name", "Component Test")
    verifier = root / VERIFIER_PATH
    verifier.parent.mkdir(parents=True)
    verifier.write_bytes((REPOSITORY_ROOT / VERIFIER_PATH).read_bytes())
    contract = {
        "schema_version": "automated-product-gate-contract-v2",
        "goal_id": "TC-VNEXT-G07-CANDIDATE",
        "gate_profile": "HARDENED_CANDIDATE_GATE",
        "isolation": {
            "mode": "FRESH_CLEAN_CHECKOUT",
            "network_access": False,
            "synthetic_profile": False,
            "authority_secret_mount_count": 0,
        },
        "checks": [
            {
                "check_id": "backend.tests",
                "argv": ["python", "-m", "pytest", "-q"],
                "workdir": "backend",
                "timeout_seconds": 600,
            }
        ],
    }
    contract_path = (
        root / "backend/eval_data/agent_gate_v1/g07_automated_product_gate.json"
    )
    _write_json(contract_path, contract)
    _git(root, "add", "--all")
    _git(root, "commit", "-m", "candidate")
    commit = _git(root, "rev-parse", "HEAD")
    tree = _git(root, "show", "-s", "--format=%T", commit)
    external = tmp_path / "external"
    external.mkdir()
    started = datetime.now(UTC).isoformat()
    argv_hash = hashlib.sha256(
        json.dumps(
            contract["checks"][0]["argv"],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    manifest_path = external / "execution.json"
    stdout_path = external / "backend.tests.stdout.log"
    stderr_path = external / "backend.tests.stderr.log"
    stdout_path.write_bytes(b"tests passed\n")
    stderr_path.write_bytes(b"")
    _write_json(
        manifest_path,
        {
            "schema_version": "automated-product-execution-manifest-v1",
            "goal_id": "TC-VNEXT-G07-CANDIDATE",
            "gate_profile": "HARDENED_CANDIDATE_GATE",
            "candidate_commit": commit,
            "candidate_tree": tree,
            "candidate_config_sha256": "1" * 64,
            "candidate_data_sha256": "2" * 64,
            "gate_contract_sha256": hashlib.sha256(
                contract_path.read_bytes()
            ).hexdigest(),
            "isolation_mode": "FRESH_CLEAN_CHECKOUT",
            "network_access": False,
            "host_mount_count": 0,
            "host_pid_namespace": False,
            "synthetic_profile": False,
            "authority_secret_mount_count": 0,
            "checks": [
                {
                    "check_id": "backend.tests",
                    "argv_sha256": argv_hash,
                    "workdir": "backend",
                    "exit_code": 0,
                    "stdout_sha256": hashlib.sha256(
                        stdout_path.read_bytes()
                    ).hexdigest(),
                    "stderr_sha256": hashlib.sha256(
                        stderr_path.read_bytes()
                    ).hexdigest(),
                    "started_at": started,
                    "completed_at": started,
                    "verdict": "PASS",
                }
            ],
            "checks_not_run": [],
            "failure_stage": None,
            "verdict": "PASS",
        },
    )
    provisional = CandidateGateComponentReceipt(
        candidate_commit=commit,
        candidate_tree=tree,
        candidate_config_sha256="1" * 64,
        candidate_data_sha256="2" * 64,
        automated_gate_contract_sha256=hashlib.sha256(
            contract_path.read_bytes()
        ).hexdigest(),
        component="AUTOMATED_PRODUCT_GATE",
        evidence_level="AUTOMATED_TEST",
        upstream_artifact_path={
            "automated.execution_manifest": str(manifest_path.resolve()),
            "automated.stdout_backend.tests": str(stdout_path.resolve()),
            "automated.stderr_backend.tests": str(stderr_path.resolve()),
        },
        upstream_artifact_sha256={
            "automated.execution_manifest": hashlib.sha256(
                manifest_path.read_bytes()
            ).hexdigest(),
            "automated.stdout_backend.tests": hashlib.sha256(
                stdout_path.read_bytes()
            ).hexdigest(),
            "automated.stderr_backend.tests": hashlib.sha256(
                stderr_path.read_bytes()
            ).hexdigest(),
        },
        verifier_sha256=hashlib.sha256(verifier.read_bytes()).hexdigest(),
        verification_summary_sha256="0" * 64,
        isolation_mode="FRESH_CLEAN_CHECKOUT",
    )
    summary = compute_candidate_component_summary(
        receipt=provisional,
        repository_root=root,
    )
    return provisional.model_copy(
        update={
            "verification_summary_sha256": verification_summary_sha256(summary)
        }
    ), root


def test_v2_receipt_reopens_raw_artifact_and_recomputes_pass(tmp_path: Path) -> None:
    receipt, root = _automated_receipt(tmp_path)

    summary = verify_candidate_component_receipt(
        receipt=receipt,
        repository_root=root,
    )

    assert summary["verdict"] == "PASS"
    assert summary["details"]["executed_check_count"] == 1


def test_v1_and_path_hash_shape_are_rejected() -> None:
    with pytest.raises(ValidationError, match="candidate-gate-component-receipt-v2"):
        CandidateGateComponentReceipt.model_validate(
            {
                "schema_version": "candidate-gate-component-receipt-v1",
                "goal_id": "TC-VNEXT-G07-CANDIDATE",
            }
        )


def test_verifier_hash_raw_hash_and_summary_hash_drift_fail_closed(
    tmp_path: Path,
) -> None:
    receipt, root = _automated_receipt(tmp_path)
    with pytest.raises(CandidateComponentVerificationError, match="verifier hash"):
        verify_candidate_component_receipt(
            receipt=receipt.model_copy(update={"verifier_sha256": "0" * 64}),
            repository_root=root,
        )
    with pytest.raises(CandidateComponentVerificationError, match="raw artifact hash"):
        verify_candidate_component_receipt(
            receipt=receipt.model_copy(
                update={
            "upstream_artifact_sha256": {
                **receipt.upstream_artifact_sha256,
                "automated.execution_manifest": "0" * 64,
                    }
                }
            ),
            repository_root=root,
        )
    with pytest.raises(CandidateComponentVerificationError, match="summary hash"):
        verify_candidate_component_receipt(
            receipt=receipt.model_copy(
                update={"verification_summary_sha256": "0" * 64}
            ),
            repository_root=root,
        )


def test_semantically_fabricated_pass_manifest_is_rejected(tmp_path: Path) -> None:
    receipt, root = _automated_receipt(tmp_path)
    path = Path(receipt.upstream_artifact_path["automated.execution_manifest"])
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["checks"][0]["check_id"] = "unrelated.fake"
    _write_json(path, payload)
    drifted = receipt.model_copy(
        update={
            "upstream_artifact_sha256": {
                **receipt.upstream_artifact_sha256,
                "automated.execution_manifest": hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
            }
        }
    )

    with pytest.raises(
        CandidateComponentVerificationError,
        match="does not recompute to PASS",
    ):
        verify_candidate_component_receipt(receipt=drifted, repository_root=root)
