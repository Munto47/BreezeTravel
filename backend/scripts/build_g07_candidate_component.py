from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from evals.agent_gate_v1.candidate_component_verifiers import (
    VERIFIER_PATH,
    compute_candidate_component_summary,
    verification_summary_sha256,
)
from evals.agent_gate_v1.candidate_gate import (
    CORE_CONFIG_ROOTS,
    CORE_DATA_ROOTS,
    _candidate_contract_binding,
    _git_blob,
    _git_bundle_sha256,
)
from evals.agent_gate_v1.contracts import (
    AgentGateComponent,
    AutomatedProductExecutionManifest,
    AutomatedProductGateContract,
    CandidateGateComponentReceipt,
    CurrentGoalBinding,
)
from evals.agent_gate_v1.path_security import (
    read_external_snapshot,
    write_external_bytes_exclusive,
)


class CandidateComponentBuildError(ValueError):
    pass


_EVIDENCE_LEVEL = {
    "AUTOMATED_PRODUCT_GATE": "AUTOMATED_TEST",
    "LIVE_PROVIDER_GATE": "LIVE_PROVIDER_EVIDENCE",
    "MULTI_AGENT_PANEL": "MULTI_AGENT_SIMULATED_REVIEW",
    "SEALED_AGENT_BLIND": "SEALED_AGENT_BLIND",
}


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise CandidateComponentBuildError(
            f"Git component build failed: {' '.join(args)}"
        )
    return result.stdout.strip()


def _artifact_argument(raw: str) -> tuple[str, Path]:
    key, separator, value = raw.partition("=")
    if not separator or not key or not value:
        raise CandidateComponentBuildError(
            "artifact arguments must use key=absolute-path"
        )
    return key, Path(value)


def _write_external(path: Path, content: bytes, root: Path) -> None:
    write_external_bytes_exclusive(path, content, root)


def run_automated_product_gate(
    *, repository_root: Path, output_root: Path
) -> dict[str, Path]:
    root = repository_root.resolve(strict=True)
    if _git(root, "status", "--porcelain=v1", "--untracked-files=all"):
        raise CandidateComponentBuildError(
            "automated component run requires a clean checkout"
        )
    commit = _git(root, "rev-parse", "HEAD")
    tree = _git(root, "show", "-s", "--format=%T", commit)
    binding = CurrentGoalBinding.model_validate_json(
        _git_blob(root, commit, "docs/governance/current_goal_binding.json")
    )
    contract_path, contract_sha256 = _candidate_contract_binding(binding)
    contract_bytes = _git_blob(root, commit, contract_path)
    contract = AutomatedProductGateContract.model_validate_json(contract_bytes)
    if (
        hashlib.sha256(contract_bytes).hexdigest() != contract_sha256
        or contract.isolation.mode != "FRESH_CLEAN_CHECKOUT"
    ):
        raise CandidateComponentBuildError(
            "automated component requires the frozen fresh-checkout contract"
        )
    output = output_root.resolve(strict=False)
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise CandidateComponentBuildError(
            "automated component output directory must be empty"
        )
    artifacts: dict[str, Path] = {}
    executions: list[dict[str, object]] = []
    for check in contract.checks:
        executable = sys.executable if check.argv[0] == "python" else shutil.which(
            check.argv[0]
        )
        if executable is None:
            raise CandidateComponentBuildError(
                f"automated component tool is unavailable: {check.argv[0]}"
            )
        started_at = datetime.now(UTC)
        try:
            result = subprocess.run(
                [executable, *check.argv[1:]],
                cwd=root / check.workdir,
                check=False,
                capture_output=True,
                timeout=check.timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise CandidateComponentBuildError(
                f"automated component timed out: {check.check_id}"
            ) from exc
        completed_at = datetime.now(UTC)
        stdout_path = output / f"{check.check_id}.stdout.log"
        stderr_path = output / f"{check.check_id}.stderr.log"
        _write_external(stdout_path, result.stdout, root)
        _write_external(stderr_path, result.stderr, root)
        artifacts[f"automated.stdout_{check.check_id}"] = stdout_path
        artifacts[f"automated.stderr_{check.check_id}"] = stderr_path
        if result.returncode != 0:
            raise CandidateComponentBuildError(
                f"automated component check failed: {check.check_id}"
            )
        executions.append(
            {
                "check_id": check.check_id,
                "argv_sha256": hashlib.sha256(
                    json.dumps(
                        check.argv,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest(),
                "workdir": check.workdir,
                "exit_code": 0,
                "stdout_sha256": hashlib.sha256(result.stdout).hexdigest(),
                "stderr_sha256": hashlib.sha256(result.stderr).hexdigest(),
                "started_at": started_at,
                "completed_at": completed_at,
                "verdict": "PASS",
            }
        )
    manifest = AutomatedProductExecutionManifest(
        goal_id=binding.goal_id,
        gate_profile=binding.gate_profile,
        candidate_commit=commit,
        candidate_tree=tree,
        candidate_config_sha256=_git_bundle_sha256(root, commit, CORE_CONFIG_ROOTS),
        candidate_data_sha256=_git_bundle_sha256(root, commit, CORE_DATA_ROOTS),
        gate_contract_sha256=contract_sha256,
        isolation_mode="FRESH_CLEAN_CHECKOUT",
        network_access=False,
        host_mount_count=0,
        host_pid_namespace=False,
        synthetic_profile=False,
        authority_secret_mount_count=0,
        checks=executions,
        checks_not_run=[],
        verdict="PASS",
    )
    manifest_path = output / "execution_manifest.json"
    _write_external(
        manifest_path,
        (
            json.dumps(
                manifest.model_dump(mode="json"),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8"),
        root,
    )
    artifacts["automated.execution_manifest"] = manifest_path
    return artifacts


def build_candidate_component_receipt(
    *,
    repository_root: Path,
    component: AgentGateComponent,
    artifact_paths: dict[str, Path],
    output_path: Path,
) -> CandidateGateComponentReceipt:
    root = repository_root.resolve(strict=True)
    if _git(root, "status", "--porcelain=v1", "--untracked-files=all"):
        raise CandidateComponentBuildError(
            "candidate component build requires a clean checkout"
        )
    commit = _git(root, "rev-parse", "HEAD")
    tree = _git(root, "show", "-s", "--format=%T", commit)
    try:
        binding = CurrentGoalBinding.model_validate_json(
            _git_blob(root, commit, "docs/governance/current_goal_binding.json")
        )
    except ValueError as exc:
        raise CandidateComponentBuildError("invalid current Goal binding") from exc
    if (
        binding.goal_id != "TC-VNEXT-G07-CANDIDATE"
        or binding.goal_sequence != 7
        or binding.gate_profile != "HARDENED_CANDIDATE_GATE"
    ):
        raise CandidateComponentBuildError("component build requires active G07")
    contract_path, contract_sha256 = _candidate_contract_binding(binding)
    if hashlib.sha256(_git_blob(root, commit, contract_path)).hexdigest() != (
        contract_sha256
    ):
        raise CandidateComponentBuildError("candidate Gate contract hash mismatch")
    snapshots = {
        key: read_external_snapshot(path, root)
        for key, path in artifact_paths.items()
    }
    verifier_sha256 = hashlib.sha256(
        _git_blob(root, commit, VERIFIER_PATH)
    ).hexdigest()
    provisional = CandidateGateComponentReceipt(
        candidate_commit=commit,
        candidate_tree=tree,
        candidate_config_sha256=_git_bundle_sha256(
            root, commit, CORE_CONFIG_ROOTS
        ),
        candidate_data_sha256=_git_bundle_sha256(root, commit, CORE_DATA_ROOTS),
        automated_gate_contract_sha256=contract_sha256,
        component=component,
        evidence_level=_EVIDENCE_LEVEL[component],
        upstream_artifact_path={
            key: str(snapshot.path) for key, snapshot in snapshots.items()
        },
        upstream_artifact_sha256={
            key: snapshot.sha256 for key, snapshot in snapshots.items()
        },
        verifier_path=VERIFIER_PATH,
        verifier_sha256=verifier_sha256,
        verification_summary_sha256="0" * 64,
        isolation_mode=(
            "FRESH_CLEAN_CHECKOUT"
            if component == "AUTOMATED_PRODUCT_GATE"
            else None
        ),
    )
    summary = compute_candidate_component_summary(
        receipt=provisional,
        repository_root=root,
    )
    receipt = provisional.model_copy(
        update={
            "verification_summary_sha256": verification_summary_sha256(summary)
        }
    )
    content = (
        json.dumps(
            receipt.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    write_external_bytes_exclusive(output_path, content, root)
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument(
        "--component",
        choices=tuple(_EVIDENCE_LEVEL),
        required=True,
    )
    parser.add_argument("--artifact", action="append", default=[])
    parser.add_argument("--run-automated-output-root", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    artifacts = dict(_artifact_argument(item) for item in args.artifact)
    if len(artifacts) != len(args.artifact):
        parser.error("artifact keys must be unique")
    if args.run_automated_output_root is not None:
        if args.component != "AUTOMATED_PRODUCT_GATE" or artifacts:
            parser.error(
                "--run-automated-output-root requires the automated component and no --artifact"
            )
        try:
            artifacts = run_automated_product_gate(
                repository_root=args.repository_root,
                output_root=args.run_automated_output_root,
            )
        except (CandidateComponentBuildError, ValueError, OSError) as exc:
            parser.error(str(exc))
    try:
        receipt = build_candidate_component_receipt(
            repository_root=args.repository_root,
            component=args.component,
            artifact_paths=artifacts,
            output_path=args.output,
        )
    except (CandidateComponentBuildError, ValueError, OSError) as exc:
        parser.error(str(exc))
    print(
        json.dumps(
            {
                "component": receipt.component,
                "candidate_commit": receipt.candidate_commit,
                "verdict": receipt.verdict,
                "output": str(args.output.resolve()),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
