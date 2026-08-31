"""Build a reproducible, secret-free release evidence manifest.

The script is intentionally honest about a dirty working tree: it records the
commit plus a diff hash and never labels uncommitted code as a clean release.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from evals.agent_gate_v1.contracts import CandidateGateComponentReceipt
from evals.agent_gate_v1.core_gate import CORE_CONFIG_ROOTS, CORE_DATA_ROOTS
from evals.agent_gate_v1.path_security import read_external_snapshot


BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent
PRODUCT_CHARTER = ROOT / "docs" / "product" / "PROJECT_CHARTER.md"
TRIP_CHECK_SPEC = ROOT / "docs" / "product" / "TRIP_CHECK_SPEC.md"
TRIP_CHECK_API_CONTRACT = ROOT / "docs" / "product" / "TRIP_CHECK_API_CONTRACT.md"
PORTFOLIO_MISSION = ROOT / "docs" / "governance" / "PORTFOLIO_MISSION.md"
PROGRAM = ROOT / "docs" / "governance" / "PROGRAM.md"
CURRENT_GOAL = ROOT / "docs" / "governance" / "CURRENT_GOAL.md"
ROADMAP = ROOT / "docs" / "governance" / "ROADMAP.md"
RELEASE_GATES = ROOT / "docs" / "governance" / "RELEASE_GATES.md"
ARCHIVED_FINAL_PLAN = (
    ROOT
    / "docs"
    / "archive"
    / "plans"
    / "BreezeTravel_双入口可验证行程产品与架构重构最终方案_2026-08-20.md"
)
LOCAL_DELIVERY_ACCEPTANCE = ROOT / "docs" / "dual-entry" / "local-delivery-acceptance-2026-08-20.md"
CAPABILITY_STATUS = ROOT / "docs" / "dual-entry" / "capability-status.md"
M1_DEV_DATASET = BACKEND / "eval_data" / "auditor_simulated" / "manifest.json"
M1_DEV_PROXY_GATE = BACKEND / "results" / "auditor_simulated" / "m1_dev_proxy_gate.json"
DUAL_ENTRY_DATASET = BACKEND / "eval_data" / "dual_entry_v1" / "manifest.json"
G5_RESTART_EVIDENCE = BACKEND / "evidence" / "full_stack" / "dual_user_backend_yjs_restart_2026-08-20.json"
FULL_BACKEND_JUNIT = BACKEND / "results" / "closure_checkpoint_20260822" / "backend-pytest.xml"
P2_RELIABILITY_GATE = (
    BACKEND / "evidence" / "trip_check_v1" / "p2" / "reliability_gate_manifest.json"
)
P3_LATEST_MIGRATION = "027_trip_intake_revision_lineage.sql"
G07_LATEST_MIGRATION = "034_trip_understanding_screenshot_batches.sql"
G07_RUN_SPEC = BACKEND / "eval_data" / "g07_candidate" / "run_spec_v1.json"
G07_VERIFICATION_MATRIX = (
    BACKEND / "eval_data" / "g07_candidate" / "verification_matrix_v1.json"
)
G07_THREAT_MODEL = BACKEND / "eval_data" / "g07_candidate" / "threat_model_v1.json"
G07_COMPONENTS = {
    "AUTOMATED_PRODUCT_GATE",
    "LIVE_PROVIDER_GATE",
    "MULTI_AGENT_PANEL",
    "SEALED_AGENT_BLIND",
}


def sha256_file(path: Path) -> str | None:
    if not path.exists():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


def config_summary() -> dict[str, object]:
    # Values are policy switches only. Credentials, URLs carrying credentials,
    # user identifiers and prompts are deliberately excluded.
    return {
        "runtime_profile": os.getenv("RUNTIME_PROFILE", "local_real"),
        "demo_mode": os.getenv("DEMO_MODE", "false").lower() == "true",
        "amap_mock": os.getenv("AMAP_MOCK", "true").lower() == "true",
        "ft_router_enabled": os.getenv("FT_ROUTER_ENABLED", "false").lower() == "true",
        "reranker_enabled": os.getenv("RERANKER_ENABLED", "false").lower() == "true",
        "auto_migrate": os.getenv("AUTO_MIGRATE", "false").lower() == "true",
        "required_migration": os.getenv(
            "REQUIRED_MIGRATION", P3_LATEST_MIGRATION
        ),
    }


def working_tree_fingerprint() -> tuple[str, int]:
    """Hash tracked changes plus untracked files without self-hashing releases."""
    digest = hashlib.sha256()
    digest.update(
        git(
            "diff",
            "--binary",
            "HEAD",
            "--",
            ".",
            ":(exclude)backend/evidence/releases/**",
        ).encode()
    )
    result = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
        capture_output=True,
        check=True,
    )
    paths = sorted(item.decode("utf-8", errors="surrogateescape") for item in result.stdout.split(b"\0") if item)
    included = 0
    for relative in paths:
        normalised = relative.replace("\\", "/")
        if normalised.startswith("backend/evidence/releases/"):
            continue
        path = ROOT / relative
        if not path.is_file():
            continue
        digest.update(normalised.encode("utf-8", errors="surrogateescape"))
        digest.update(b"\0")
        digest.update((sha256_file(path) or "").encode())
        included += 1
    return digest.hexdigest(), included


def evidence_reference(path: Path) -> dict[str, object]:
    """Return a portable, hash-bound reference without reading private payloads."""
    return {
        "path": path.relative_to(ROOT).as_posix(),
        "exists": path.exists(),
        "sha256": sha256_file(path),
    }


def latest_product_gate(phase: str) -> Path | None:
    """Return the newest durable gate for one concrete public-HTTP phase."""

    candidates: list[tuple[float, Path]] = []
    for path in (BACKEND / "evidence" / "runs").glob("*/gate.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("phase") != phase:
                continue
            completed = payload.get("completed_at_epoch", path.stat().st_mtime)
            candidates.append((float(completed), path))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            continue
    return max(candidates, default=(0.0, None), key=lambda item: item[0])[1]


def optional_evidence_reference(path: Path | None) -> dict[str, object]:
    if path is None:
        return {"path": None, "exists": False, "sha256": None}
    return evidence_reference(path)


def gate_summary(path: Path | None, *, current_tree_hash: str) -> dict[str, object]:
    if path is None or not path.is_file():
        return {
            "decision": "NOT_RUN",
            "status": "UNAVAILABLE",
            "same_worktree_binding": False,
        }
    payload = json.loads(path.read_text(encoding="utf-8"))
    bound_diff = payload.get("bindings", {}).get("dirty_diff_sha256")
    summary = {
        "run_id": payload.get("run_id"),
        "phase": payload.get("phase"),
        "decision": payload.get("decision"),
        "status": payload.get("status"),
        "same_worktree_binding": bool(bound_diff and bound_diff == current_tree_hash),
        "bound_dirty_diff_sha256": bound_diff,
        "failed_case_count": len(payload.get("failed_cases") or []),
    }
    if payload.get("phase") == "BUILDER_SUGGESTION_HTTP":
        session_gate = next(
            (
                item
                for item in payload.get("gates", [])
                if isinstance(item, dict) and item.get("id") == "G2_FOUR_STOP_SESSION_COUNT"
            ),
            None,
        )
        session_gate_passed = bool(
            session_gate
            and session_gate.get("status") == "PASS"
            and isinstance(session_gate.get("actual"), int)
            and isinstance(session_gate.get("threshold"), int)
            and session_gate["actual"] >= session_gate["threshold"]
        )
        summary["reported_decision"] = payload.get("decision")
        summary["g2_four_stop_session_gate"] = session_gate or {
            "status": "MISSING",
            "reason": "LEGACY_GATE_DID_NOT_PROVE_REQUIRED_SESSION_COUNT",
        }
        if not session_gate_passed:
            summary["decision"] = "REJECT"
            summary["status"] = "INVALID"
    return summary


def build(output_root: Path, *, require_clean: bool = False) -> Path:
    commit = git("rev-parse", "HEAD")
    status = git("status", "--porcelain=v1")
    dirty = bool(status)
    if require_clean and dirty:
        raise RuntimeError("RC1 candidate manifest requires a clean working tree")
    tree_hash, untracked_count = working_tree_fingerprint() if dirty else ("", 0)
    import_gate = latest_product_gate("IMPORT_HTTP")
    builder_gate = latest_product_gate("BUILDER_SUGGESTION_HTTP")
    dataset_payload = json.loads(DUAL_ENTRY_DATASET.read_text(encoding="utf-8")) if DUAL_ENTRY_DATASET.is_file() else {}
    restart_payload = (
        json.loads(G5_RESTART_EVIDENCE.read_text(encoding="utf-8")) if G5_RESTART_EVIDENCE.is_file() else {}
    )
    import_summary = gate_summary(import_gate, current_tree_hash=tree_hash)
    builder_summary = gate_summary(builder_gate, current_tree_hash=tree_hash)
    legacy_release_blockers = list(dataset_payload.get("release_blockers") or [])
    if import_summary["decision"] not in {"PROMOTE", "ACCEPT_IMPORT_HTTP_SLICE"}:
        legacy_release_blockers.append("LEGACY_IMPORT_HTTP_GATE_NOT_PROMOTED")
    if builder_summary["decision"] != "PROMOTE":
        legacy_release_blockers.append("LEGACY_BUILDER_HTTP_GATE_NOT_PROMOTED")
    if not import_summary["same_worktree_binding"] or not builder_summary["same_worktree_binding"]:
        legacy_release_blockers.append("LEGACY_HTTP_GATE_WORKTREE_BINDING_STALE_OR_MISSING")
    trip_check_release_blockers = [
        "TRIP_CHECK_V1_P3_INPUT_PROVIDER_NOT_PASSED",
        "TRIP_CHECK_V1_DATASET_360_NOT_BUILT",
        "G1_OFFLINE_NOT_RUN_FOR_CANDIDATE",
        "G2_POSTGRES_NOT_RUN_FOR_CANDIDATE",
        "G3_SNAPSHOT_NOT_RUN_FOR_CANDIDATE",
        "G4_LIVE_PROVIDERS_NOT_RUN_FOR_CANDIDATE",
        "G5_BROWSER_AND_PERFORMANCE_NOT_RUN_FOR_CANDIDATE",
        "G6_RELEASE_CANDIDATE_MANIFEST_NOT_APPROVED",
    ]
    release_id = f"{commit[:12]}-dirty-{tree_hash[:12]}" if dirty else commit
    all_migrations = sorted((BACKEND / "app" / "db" / "migrations").glob("*.sql"))
    try:
        p3_cutoff = next(
            index
            for index, migration in enumerate(all_migrations)
            if migration.name == P3_LATEST_MIGRATION
        )
    except StopIteration as exc:
        raise RuntimeError(
            f"Trip Check V1 P3 manifest requires {P3_LATEST_MIGRATION}"
        ) from exc
    # This builder is the frozen P3 evidence surface. Later product migrations
    # must not silently rewrite the historical manifest's authority boundary.
    migrations = all_migrations[: p3_cutoff + 1]
    payload = {
        "schema_version": "4.0",
        "release_status": "trip_check_v1_p3_input_provider_draft",
        "release_approval_granted": False,
        "local_delivery_test_execution": "not_run_by_manifest",
        "manifest_generation_executes_tests": False,
        "release_id": release_id,
        "commit_sha": commit,
        "working_tree_clean": not dirty,
        "working_tree_diff_sha256": tree_hash if dirty else None,
        "untracked_files_hashed": untracked_count,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "toolchains": {"python": "3.11", "node": "20", "postgres_pgvector": "0.8.1-pg16", "redis": "7.4.2"},
        "dependency_locks": {
            "backend_requirements": sha256_file(BACKEND / "requirements.txt"),
            "backend_dev_requirements": sha256_file(BACKEND / "requirements-dev.txt"),
            "frontend_package_lock": sha256_file(ROOT / "frontend" / "package-lock.json"),
            "yjs_package_lock": sha256_file(ROOT / "y-websocket" / "package-lock.json"),
        },
        "migrations": [{"name": item.name, "sha256": sha256_file(item)} for item in migrations],
        "latest_migration": P3_LATEST_MIGRATION,
        "configuration": config_summary(),
        "evaluation_scope": {
            "supported_and_claimed_cities": ["北京", "上海", "杭州"],
            "target_cases_per_city": 120,
            "target_total_cases": 360,
            "target_splits": {"pilot": 18, "dev": 180, "regression": 72, "frozen_blind": 90},
            "dataset_status": "pilot_18_revalidated_p2_pass_dev_not_started",
            "expanded_city_claims": False,
        },
        "product_authority": {
            "agents": evidence_reference(ROOT / "AGENTS.md"),
            "project_charter": evidence_reference(PRODUCT_CHARTER),
            "trip_check_spec": evidence_reference(TRIP_CHECK_SPEC),
            "trip_check_api_contract": evidence_reference(TRIP_CHECK_API_CONTRACT),
            "portfolio_mission": evidence_reference(PORTFOLIO_MISSION),
            "program": evidence_reference(PROGRAM),
            "current_goal": evidence_reference(CURRENT_GOAL),
            "roadmap": evidence_reference(ROADMAP),
            "release_gates": evidence_reference(RELEASE_GATES),
            "capability_status": evidence_reference(CAPABILITY_STATUS),
            "authority_order": [
                "AGENTS.md",
                "PROJECT_CHARTER.md",
                "TRIP_CHECK_SPEC.md",
                "PORTFOLIO_MISSION.md",
                "PROGRAM.md",
                "CURRENT_GOAL.md/ROADMAP.md/RELEASE_GATES.md",
                "ADR",
                "same-commit evidence",
            ],
        },
        "trip_check_v1_release_gate_evidence": {
            "g0_document_and_schema": "CONTRACT_PRESENT_RUNTIME_GATE_NOT_RUN",
            "g1_offline": "NOT_RUN",
            "g2_postgres": "NOT_RUN",
            "g3_fixed_snapshot": "NOT_RUN",
            "g4_live_providers": "NOT_RUN",
            "g5_browser_recovery_performance": "NOT_RUN",
            "g6_release_manifest": "BASELINE_ONLY",
            "p2_reliability_gate": evidence_reference(P2_RELIABILITY_GATE),
            "p2_reliability_status": "PASS_CONTROLLED_POSTGRES_BROWSER",
            "automated_proxy_judge": "NOT_RUN",
            "human_validation": "OUT_OF_SCOPE_UNTIL_SEPARATELY_APPROVED",
            "overall_release_decision": "REJECT",
            "release_blockers": trip_check_release_blockers,
            "claim_boundary": (
                "This manifest records P1 D1 and P2 Reliability as controlled evidence while P3 remains DRAFT. "
                "It does not prove candidate-commit G0-G6, live-provider behavior, public browser acceptance, "
                "human validation, or release approval."
            ),
        },
        "legacy_dual_entry_delivery_evidence": {
            "archived_final_plan": evidence_reference(ARCHIVED_FINAL_PLAN),
            "capability_status": evidence_reference(CAPABILITY_STATUS),
            "local_delivery_acceptance": evidence_reference(LOCAL_DELIVERY_ACCEPTANCE),
            "m1_dev_dataset_manifest": evidence_reference(M1_DEV_DATASET),
            "m1_dev_proxy_gate": evidence_reference(M1_DEV_PROXY_GATE),
            "m1_dev_evidence_type": "synthetic_proxy",
            "human_validated": False,
            "publicly_verified": False,
            "claim_boundary": (
                "The M1-dev proxy gate is a local development gate only. "
                "It is not human validation, live-provider proof, public deployment proof, "
                "or release approval."
            ),
        },
        "legacy_dual_entry_release_gate_evidence": {
            "dataset_manifest": evidence_reference(DUAL_ENTRY_DATASET),
            "latest_import_http_gate": optional_evidence_reference(import_gate),
            "latest_builder_http_gate": optional_evidence_reference(builder_gate),
            "g5_restart_evidence": evidence_reference(G5_RESTART_EVIDENCE),
            "full_backend_junit": evidence_reference(FULL_BACKEND_JUNIT),
            "dataset_release_eligible": dataset_payload.get("release_eligible", False),
            "import_http": import_summary,
            "builder_http": builder_summary,
            "g5_restart_status": restart_payload.get("status", "UNAVAILABLE"),
            "weekly_live_decision": "NOT_RUN",
            "independent_paired_judge_decision": "NOT_RUN",
            "baseline_candidate_promotion_decision": "NOT_RUN",
            "external_blind_bundle_provisioned": False,
            "human_calibration_case_count": 0,
            "overall_release_decision": "REJECT",
            "release_blockers": list(dict.fromkeys(legacy_release_blockers)),
            "claim_boundary": (
                "This section is an evidence index, not a release approval. "
                "A stale worktree binding, missing stage, failed gate, absent blind bundle, "
                "or zero human calibration keeps the overall decision REJECT."
            ),
        },
        "external_api_policy": {
            "iteration_mode": "frozen_snapshot_zero_external_calls",
            "paid_live_runs_executed": 0,
            "paid_live_runs_status": "paused",
            "paid_generation_requires_explicit_flag": True,
            "api_llm_judge_allowed": False,
        },
        "legacy_rc1_judge_policy": {
            "kind": "independent_codex_subagent_panel",
            "model": "gpt-5.6-sol",
            "evaluator_count": 3,
            "minimum_unanimous_agreement": 0.85,
            "human_calibration_performed": False,
            "agreement_threshold_passed": True,
            "quality_thresholds_passed": False,
            "allowed_claim": "历史 RC1 Judge 记录；不替代双入口 M1-dev 代理门禁或真人校准",
        },
        "evaluation_manifest_sha256": sha256_file(M1_DEV_DATASET),
        "evidence_paths": {
            "local_eval": "backend/evidence/local_eval/summary.json",
            "fault_injection": "backend/evidence/fault_injection/summary.json",
            "experiments": "backend/evidence/experiments/summary.json",
            "multi_instance": "backend/evidence/multi_instance/summary.json",
            "three_city_rc1": "backend/evidence/three_city_rc1/summary.json",
        },
        "verification_commands": [
            "powershell -ExecutionPolicy Bypass -File .\\verify-local.ps1",
            "docker compose config --quiet",
            "docker compose -f docker-compose.multi.yml config --quiet",
        ],
        "trip_check_v1_verification_commands": [
            "cd backend; python -m pytest tests -q",
            "cd backend; python -m ruff check app tests scripts",
            "cd frontend; npm run build",
            "python backend/scripts/validate_dual_entry_testset.py",
        ],
        "excluded_claims": [
            "Trip Check V1 implemented",
            "Trip Check V1 release candidate",
            "public deployment",
            "public smoke",
            "real-user validation",
            "human calibration",
            "automated Judge as human evidence",
            "live-provider SLO",
            "production SLO",
        ],
    }
    target = output_root / release_id / "release.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(target)
    latest = output_root / "latest.json"
    try:
        manifest_reference = target.relative_to(ROOT).as_posix()
        manifest_reference_kind = "workspace_relative"
    except ValueError:
        # A caller may deliberately put artifacts outside the workspace (for
        # example, a CI temporary directory).  Preserve a usable pointer
        # rather than turning a successful evidence build into a path error.
        manifest_reference = str(target.resolve())
        manifest_reference_kind = "absolute_external"
    latest.write_text(
        json.dumps(
            {
                "release_id": release_id,
                "manifest": manifest_reference,
                "manifest_reference_kind": manifest_reference_kind,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return target


def _verified_g07_bindings(run_spec: dict[str, object]) -> list[dict[str, object]]:
    verified: list[dict[str, object]] = []
    sections = ("contract_bindings", "evaluation_bindings")
    for section_name in sections:
        section = run_spec.get(section_name)
        if not isinstance(section, dict):
            raise RuntimeError(f"G07 RunSpec is missing {section_name}")
        for binding_name, raw_binding in section.items():
            if not isinstance(raw_binding, dict):
                raise RuntimeError(f"G07 binding is invalid: {binding_name}")
            path_value = raw_binding.get("path")
            expected_sha256 = raw_binding.get("sha256")
            if not isinstance(path_value, str) or not isinstance(expected_sha256, str):
                raise RuntimeError(f"G07 binding is incomplete: {binding_name}")
            path = ROOT / path_value
            observed_sha256 = sha256_file(path)
            if observed_sha256 != expected_sha256:
                raise RuntimeError(f"G07 binding hash mismatch: {binding_name}")
            verified.append(
                {
                    "binding": binding_name,
                    "path": path_value,
                    "sha256": observed_sha256,
                    "section": section_name,
                }
            )
    return sorted(verified, key=lambda item: (str(item["section"]), str(item["binding"])))


def _g07_git_bundle_sha256(commit: str, roots: tuple[str, ...]) -> str:
    raw = git("ls-tree", "-r", "--full-tree", commit, "--", *roots)
    entries: list[list[str]] = []
    for line in raw.splitlines():
        metadata, path = line.split("\t", maxsplit=1)
        _mode, object_type, object_id = metadata.split()
        if object_type == "blob":
            entries.append([path, object_type, object_id])
    if not entries:
        raise RuntimeError("G07 candidate Git bundle resolved no blobs")
    return hashlib.sha256(
        json.dumps(
            sorted(entries),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _verified_g07_components(
    paths: list[Path],
    *,
    commit: str,
    tree: str,
    automated_contract_sha256: str,
) -> dict[str, dict[str, object]]:
    if not paths:
        return {}
    if len(paths) != len(G07_COMPONENTS):
        raise RuntimeError("G07 manifest requires exactly four component receipts")
    config_sha256 = _g07_git_bundle_sha256(commit, CORE_CONFIG_ROOTS)
    data_sha256 = _g07_git_bundle_sha256(commit, CORE_DATA_ROOTS)
    verified: dict[str, dict[str, object]] = {}
    for path in paths:
        snapshot = read_external_snapshot(path, ROOT)
        try:
            receipt = CandidateGateComponentReceipt.model_validate_json(snapshot.content)
        except ValueError as exc:
            raise RuntimeError(f"invalid G07 component receipt: {path.name}") from exc
        if receipt.component in verified:
            raise RuntimeError("G07 manifest received a duplicate component receipt")
        if (
            receipt.candidate_commit != commit
            or receipt.candidate_tree != tree
            or receipt.candidate_config_sha256 != config_sha256
            or receipt.candidate_data_sha256 != data_sha256
            or receipt.automated_gate_contract_sha256
            != automated_contract_sha256
        ):
            raise RuntimeError("G07 component receipt candidate binding mismatch")
        verified[receipt.component] = {
            "receipt_sha256": snapshot.sha256,
            "evidence_level": receipt.evidence_level,
            "upstream_artifact_sha256": dict(
                sorted(receipt.upstream_artifact_sha256.items())
            ),
            "verifier_sha256": receipt.verifier_sha256,
            "isolation_mode": receipt.isolation_mode,
            "human_evidence": False,
            "production_evidence": False,
        }
    if set(verified) != G07_COMPONENTS:
        raise RuntimeError("G07 manifest component receipt set is incomplete")
    return dict(sorted(verified.items()))


def build_g07_candidate_manifest(
    output_root: Path,
    *,
    require_clean: bool = False,
    component_receipt_paths: list[Path] | None = None,
) -> Path:
    """Build a fail-closed TC-VNEXT G07 manifest without reusing legacy proof."""

    commit = git("rev-parse", "HEAD")
    tree = git("show", "-s", "--format=%T", "HEAD")
    status = git("status", "--porcelain=v1", "--untracked-files=all")
    dirty = bool(status)
    if require_clean and dirty:
        raise RuntimeError("G07 candidate manifest requires a clean working tree")
    dirty_fingerprint, _ = working_tree_fingerprint() if dirty else ("", 0)
    run_spec = json.loads(G07_RUN_SPEC.read_text(encoding="utf-8"))
    matrix = json.loads(G07_VERIFICATION_MATRIX.read_text(encoding="utf-8"))
    threat_model = json.loads(G07_THREAT_MODEL.read_text(encoding="utf-8"))
    if run_spec.get("schema_version") != "g07-candidate-run-spec-v1":
        raise RuntimeError("unsupported G07 RunSpec")
    if matrix.get("schema_version") != "g07-verification-matrix-v1":
        raise RuntimeError("unsupported G07 verification matrix")
    if threat_model.get("schema_version") != "g07-candidate-threat-model-v1":
        raise RuntimeError("unsupported G07 threat model")
    if any(
        value.get("goal_id") != "TC-VNEXT-G07-CANDIDATE"
        for value in (run_spec, matrix, threat_model)
    ):
        raise RuntimeError("G07 candidate artifacts disagree on Goal")
    verified_bindings = _verified_g07_bindings(run_spec)
    automated_contract_sha256 = next(
        str(item["sha256"])
        for item in verified_bindings
        if item["binding"] == "automated_candidate_gate"
    )
    verified_components = _verified_g07_components(
        component_receipt_paths or [],
        commit=commit,
        tree=tree,
        automated_contract_sha256=automated_contract_sha256,
    )
    components_complete = set(verified_components) == G07_COMPONENTS
    gates = matrix.get("gates")
    if not isinstance(gates, list) or [item.get("gate_id") for item in gates] != [
        f"G{index}" for index in range(9)
    ]:
        raise RuntimeError("G07 verification matrix must define G0 through G8")
    gate_status = {str(item["gate_id"]): str(item["status"]) for item in gates}
    if any(status_value not in {"NOT_RUN", "NOT_READY", "PASS", "FAIL"} for status_value in gate_status.values()):
        raise RuntimeError("G07 verification matrix contains an invalid status")
    # This builder freezes and discloses the candidate inputs.  It deliberately
    # cannot mint a PASS from editable matrix status fields; final G07 PASS is
    # produced only by the candidate Gate from independently verified receipts.
    all_pass = False
    blockers = [
        f"{gate_id}_{status_value}"
        for gate_id, status_value in gate_status.items()
        if status_value != "PASS"
    ]
    if dirty:
        blockers.insert(0, "WORKING_TREE_NOT_CLEAN")
    if not components_complete:
        blockers.append("G07_COMPONENT_RECEIPTS_NOT_RUN")
    migrations = sorted((BACKEND / "app" / "db" / "migrations").glob("*.sql"))
    if not migrations or migrations[-1].name != G07_LATEST_MIGRATION:
        raise RuntimeError(f"G07 manifest requires latest migration {G07_LATEST_MIGRATION}")
    release_id = (
        f"g07-{commit}-dirty-{dirty_fingerprint[:12]}"
        if dirty
        else f"g07-{commit}"
    )
    payload = {
        "schema_version": "tc-vnext-g07-candidate-manifest-v1",
        "goal_id": "TC-VNEXT-G07-CANDIDATE",
        "release_id": release_id,
        "candidate_subject": {
            "commit": commit,
            "tree": tree,
            "remote_ref": "refs/heads/codex/g07-candidate",
            "working_tree_clean": not dirty,
        },
        "created_at": datetime.now(timezone.utc).isoformat(),
        "manifest_generation_executes_tests": False,
        "candidate_status": (
            "VNEXT_CANDIDATE_READY_AGENT_VERIFIED"
            if all_pass
            else "CANDIDATE_EVIDENCE_INCOMPLETE"
        ),
        "candidate_gate_passed": all_pass,
        "manifest_gate_status": "PASS" if components_complete else "NOT_RUN",
        "release_approval_granted": False,
        "deployment_requested": False,
        "main_merge_requested": False,
        "run_spec": evidence_reference(G07_RUN_SPEC),
        "verification_matrix": evidence_reference(G07_VERIFICATION_MATRIX),
        "threat_model": evidence_reference(G07_THREAT_MODEL),
        "verified_input_bindings": verified_bindings,
        "component_receipts": verified_components,
        "component_receipt_sha256": {
            component: details["receipt_sha256"]
            for component, details in verified_components.items()
        },
        "latest_migration": G07_LATEST_MIGRATION,
        "migrations": [
            {"name": migration.name, "sha256": sha256_file(migration)}
            for migration in migrations
        ],
        "gate_status": gate_status,
        "release_blockers": blockers,
        "historical_delivery_receipts": {
            goal: evidence_reference(
                ROOT / "docs" / "governance" / "gate-results" / f"{goal}.product-delivery.json"
            )
            for goal in ("G04", "G05", "G06")
        },
        "exact_binding_receipt": evidence_reference(
            ROOT / "docs" / "governance" / "gate-results" / "G07.exact-binding.json"
        ),
        "candidate_materials": {
            "controlled_demo": "NOT_RUN",
            "video_90_seconds": "NOT_RUN",
            "demo_script_5_minutes": "NOT_RUN",
            "architecture_diagram": "NOT_RUN",
            "recovery_sequence": "NOT_RUN",
            "model_ablation": "NOT_RUN",
            "known_boundaries": "RUNSPEC_BOUND",
        },
        "evidence_boundaries": {
            "fixture": "SEPARATE",
            "snapshot": "SEPARATE",
            "live_provider": (
                "VERIFIED_COMPONENT_RECEIPT"
                if "LIVE_PROVIDER_GATE" in verified_components
                else "NOT_RUN"
            ),
            "browser": "NOT_RUN",
            "multi_agent": (
                "VERIFIED_COMPONENT_RECEIPT"
                if "MULTI_AGENT_PANEL" in verified_components
                else "NOT_RUN"
            ),
            "sealed_blind": (
                "VERIFIED_COMPONENT_RECEIPT"
                if "SEALED_AGENT_BLIND" in verified_components
                else "NOT_RUN"
            ),
            "human_usability": "NOT_RUN",
            "public_network": "NOT_RUN",
            "production": "NOT_RUN",
            "commercial": "NOT_RUN",
        },
        "claim_boundary": (
            "This manifest is a fail-closed G07 evidence index. Historical delivery "
            "receipts do not satisfy current candidate gates. Agent-verified candidate "
            "status does not prove H1, public network, production, commercial, release, "
            "deployment or main-branch approval."
        ),
    }
    target = output_root / release_id / "release.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise RuntimeError("G07 candidate manifest already exists for this subject")
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    latest = output_root / "latest.json"
    try:
        manifest_reference = target.relative_to(ROOT).as_posix()
        manifest_reference_kind = "workspace_relative"
    except ValueError:
        manifest_reference = str(target.resolve())
        manifest_reference_kind = "absolute_external"
    latest.write_text(
        json.dumps(
            {
                "release_id": release_id,
                "manifest": manifest_reference,
                "manifest_reference_kind": manifest_reference_kind,
                "sha256": sha256_file(target),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return target


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=BACKEND / "evidence" / "releases")
    parser.add_argument("--require-clean", action="store_true")
    parser.add_argument("--profile", choices=("legacy", "g07"), default="legacy")
    parser.add_argument("--component", action="append", type=Path, default=[])
    args = parser.parse_args()
    builder = build_g07_candidate_manifest if args.profile == "g07" else build
    if args.profile == "legacy" and args.component:
        parser.error("--component is only valid with --profile g07")
    if args.profile == "g07":
        print(
            builder(
                args.output,
                require_clean=args.require_clean,
                component_receipt_paths=args.component,
            )
        )
    else:
        print(builder(args.output, require_clean=args.require_clean))
