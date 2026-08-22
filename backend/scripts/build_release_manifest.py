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


BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent
FINAL_PLAN = ROOT / "docs" / "BreezeTravel_双入口可验证行程产品与架构重构最终方案_2026-08-20.md"
LOCAL_DELIVERY_ACCEPTANCE = ROOT / "docs" / "dual-entry" / "local-delivery-acceptance-2026-08-20.md"
CAPABILITY_STATUS = ROOT / "docs" / "dual-entry" / "capability-status.md"
M1_DEV_DATASET = BACKEND / "eval_data" / "auditor_simulated" / "manifest.json"
M1_DEV_PROXY_GATE = BACKEND / "results" / "auditor_simulated" / "m1_dev_proxy_gate.json"
DUAL_ENTRY_DATASET = BACKEND / "eval_data" / "dual_entry_v1" / "manifest.json"
G5_RESTART_EVIDENCE = (
    BACKEND / "evidence" / "full_stack" / "dual_user_backend_yjs_restart_2026-08-20.json"
)
FULL_BACKEND_JUNIT = BACKEND / "results" / "dual_entry_full_20260821.xml"


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
        ["git", *args], cwd=ROOT, text=True, encoding="utf-8",
        errors="replace", capture_output=True, check=True,
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
        "required_migration": os.getenv("REQUIRED_MIGRATION", "021_atomic_suggestion_undo.sql"),
    }


def working_tree_fingerprint() -> tuple[str, int]:
    """Hash tracked changes plus untracked files without self-hashing releases."""
    digest = hashlib.sha256()
    digest.update(git("diff", "--binary", "HEAD").encode())
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
    return {
        "run_id": payload.get("run_id"),
        "phase": payload.get("phase"),
        "decision": payload.get("decision"),
        "status": payload.get("status"),
        "same_worktree_binding": bool(bound_diff and bound_diff == current_tree_hash),
        "bound_dirty_diff_sha256": bound_diff,
        "failed_case_count": len(payload.get("failed_cases") or []),
    }


def build(output_root: Path, *, require_clean: bool = False) -> Path:
    commit = git("rev-parse", "HEAD")
    status = git("status", "--porcelain=v1")
    dirty = bool(status)
    if require_clean and dirty:
        raise RuntimeError("RC1 candidate manifest requires a clean working tree")
    tree_hash, untracked_count = working_tree_fingerprint() if dirty else ("", 0)
    import_gate = latest_product_gate("IMPORT_HTTP")
    builder_gate = latest_product_gate("BUILDER_SUGGESTION_HTTP")
    dataset_payload = (
        json.loads(DUAL_ENTRY_DATASET.read_text(encoding="utf-8"))
        if DUAL_ENTRY_DATASET.is_file()
        else {}
    )
    restart_payload = (
        json.loads(G5_RESTART_EVIDENCE.read_text(encoding="utf-8"))
        if G5_RESTART_EVIDENCE.is_file()
        else {}
    )
    import_summary = gate_summary(import_gate, current_tree_hash=tree_hash)
    builder_summary = gate_summary(builder_gate, current_tree_hash=tree_hash)
    release_blockers = list(dataset_payload.get("release_blockers") or [])
    if import_summary["decision"] not in {"PROMOTE", "ACCEPT_IMPORT_HTTP_SLICE"}:
        release_blockers.append("LATEST_IMPORT_HTTP_GATE_NOT_PROMOTED")
    if builder_summary["decision"] != "PROMOTE":
        release_blockers.append("LATEST_BUILDER_HTTP_GATE_NOT_PROMOTED")
    if not import_summary["same_worktree_binding"] or not builder_summary["same_worktree_binding"]:
        release_blockers.append("HTTP_GATE_WORKTREE_BINDING_STALE_OR_MISSING")
    if restart_payload.get("status") != "PASSED":
        release_blockers.append("G5_RESTART_EVIDENCE_NOT_PASSED")
    release_blockers.extend(
        [
            "G3_INDEPENDENT_PAIRED_JUDGE_NOT_RUN",
            "BASELINE_CANDIDATE_PROMOTION_NOT_RUN",
        ]
    )
    release_id = f"{commit[:12]}-dirty-{tree_hash[:12]}" if dirty else commit
    migrations = sorted((BACKEND / "app" / "db" / "migrations").glob("*.sql"))
    payload = {
        "schema_version": "3.0",
        "release_status": "dual_entry_local_delivery_candidate",
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
        "latest_migration": migrations[-1].name if migrations else None,
        "configuration": config_summary(),
        "evaluation_scope": {
            "supported_and_claimed_cities": ["北京", "上海", "杭州"],
            "cases_per_city": 50,
            "total_cases": 150,
            "expanded_city_claims": False,
        },
        "dual_entry_delivery_evidence": {
            "final_plan": evidence_reference(FINAL_PLAN),
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
        "dual_entry_release_gate_evidence": {
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
            "release_blockers": list(dict.fromkeys(release_blockers)),
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
        "dual_entry_local_delivery_verification_commands": [
            "cd backend; python -m pytest tests -q",
            "cd backend; python -m ruff check app evals scripts tests",
            (
                "cd backend; python -m scripts.run_m1_dev_proxy_gate "
                "--artifact results/auditor_simulated/proxy_role_1.json "
                "--artifact results/auditor_simulated/proxy_role_2.json "
                "--artifact results/auditor_simulated/proxy_role_3.json"
            ),
            "cd frontend; npm run build",
            "cd frontend; npx playwright test -c playwright.local.config.js",
            "cd frontend; npx playwright test -c playwright.workspace.config.js",
            (
                "docker compose up -d postgres; cd backend; "
                "$env:RUN_SERVICE_INTEGRATION='1'; python -m pytest "
                "tests/test_migrations_integration.py tests/test_templates_sharing_postgres.py "
                "tests/test_dual_entry_postgres_integration.py -q; docker compose stop postgres"
            ),
        ],
        "excluded_claims": [
            "full RC1 release", "public deployment", "public smoke", "real-user validation",
            "human calibration", "Judge-human agreement", "live-provider SLO", "production SLO",
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


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=BACKEND / "evidence" / "releases")
    parser.add_argument("--require-clean", action="store_true")
    args = parser.parse_args()
    print(build(args.output, require_clean=args.require_clean))
