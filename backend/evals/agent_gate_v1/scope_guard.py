from __future__ import annotations

import ast
import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from evals.agent_gate_v1.contracts import (
    ActiveSlice,
    ScopeGuardErrorCode,
    ScopeGuardVerdict,
    ScopePhase,
    WorkPackageRegistry,
)


class ScopeGuardError(ValueError):
    pass


class ScopeGuardReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["scope-guard-report-v1"] = "scope-guard-report-v1"
    mode: Literal["ENFORCE", "AUDIT"]
    slice_id: str
    base_commit: str
    observed_head: str
    declared_phase: ScopePhase
    requested_phase: ScopePhase | None
    work_kind: str
    verdict: ScopeGuardVerdict
    error_codes: list[ScopeGuardErrorCode]
    changed_files: list[str]
    non_generated_file_count: int
    handwritten_added_lines: int
    new_schema_count: int
    classifications: dict[str, list[str]]
    detected_mechanisms: list[str]
    product_progress: list[str]
    invalidated_evidence: list[str]
    required_action: str


MAX_NON_GENERATED_FILES = 5
MAX_HANDWRITTEN_ADDED_LINES = 300
MAX_NEW_SCHEMAS = 2
POLICY_PATHS = {
    "AGENTS.md",
    ".github/workflows/ci.yml",
    "backend/evals/agent_gate_v1/contracts.py",
    "backend/evals/agent_gate_v1/scope_guard.py",
    "backend/evals/agent_gate_v1/work_packages.py",
    "backend/scripts/build_agent_gate_pass.py",
    "backend/scripts/validate_work_packages.py",
    "backend/eval_data/agent_gate_v1/work_package_registry.schema.json",
    "docs/governance/AGENT_GATE_PROTOCOL.md",
    "docs/governance/PRODUCT_MAINLINE_EXECUTION_GUIDE.md",
    "docs/governance/PROGRAM.md",
    "docs/governance/current_goal_binding.json",
}
POLICY_HASH_PATHS = tuple(
    sorted(
        POLICY_PATHS
        - {
            "backend/eval_data/agent_gate_v1/work_package_registry.schema.json",
            "docs/governance/PROGRAM.md",
            "docs/governance/current_goal_binding.json",
        }
    )
)
CURRENT_GATE_POLICY_FIX_PATHS = {
    "backend/evals/agent_gate_v1/contracts.py",
    "backend/evals/agent_gate_v1/scope_guard.py",
    "backend/eval_data/agent_gate_v1/work_package_registry.schema.json",
}
DEPENDENCY_PATHS = {
    "backend/requirements-base.txt",
    "backend/requirements-dev.txt",
    "backend/requirements.txt",
    "backend/pyproject.toml",
    "frontend/package.json",
    "frontend/package-lock.json",
    "miniapp/package.json",
    "miniapp/package-lock.json",
}
_DURABLE_PATTERN = re.compile(
    r"\b(sqlite3|CREATE\s+TABLE|create_table|custody_registry|mint_receipt|sealed_runs)\b",
    re.IGNORECASE,
)
_CRYPTO_PATTERN = re.compile(
    r"\b(import\s+hmac|hmac\.new|from\s+cryptography|ed25519|signing_key|"
    r"authority_signature|sign_receipt|verify_signature)\b",
    re.IGNORECASE,
)
_HARDENING_PATH_PATTERN = re.compile(r"(custody|authority|broker|registry|mint)", re.I)
_PASS_FIELD_PATTERN = re.compile(r"(pass|consumed|verified|valid|ready)", re.I)
_HANDWRITTEN_CODE_SUFFIXES = {".py", ".sql", ".js", ".jsx", ".ts", ".tsx"}


def _git(root: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if check and result.returncode != 0:
        raise ScopeGuardError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout.strip()


def _git_succeeds(root: Path, *args: str) -> bool:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=False,
        capture_output=True,
    )
    return result.returncode == 0


def _contains(root: str, path: str) -> bool:
    return path == root or path.startswith(f"{root}/")


def scope_policy_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for relative in POLICY_HASH_PATHS:
        path = root / relative
        if not path.is_file():
            raise ScopeGuardError(f"scope policy file is missing: {relative}")
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _is_generated(path: str) -> bool:
    return (
        path.endswith(".schema.json")
        or path == "backend/eval_data/agent_gate_v1/protocol_contract.json"
        or "/src/generated/" in f"/{path}"
        or path.endswith("openapi.json")
        or path.endswith("openapi.current.json")
    )


def _classify(path: str) -> str:
    if "/tests/" in f"/{path}" or Path(path).name.startswith("test_"):
        return "tests"
    if path == "AGENTS.md" or path.startswith(("docs/governance/", ".github/")):
        return "governance"
    if path.startswith(("backend/evals/", "backend/eval_data/", "backend/scripts/")):
        return "eval_infra"
    if path.startswith(("frontend/", "miniapp/")):
        return "ui_runtime"
    if path.startswith("backend/app/") and "/migrations/" not in f"/{path}":
        return "api_runtime" if "/api/" in f"/{path}" else "product_runtime"
    if path.startswith("packages/trip-check-client/src/"):
        return "api_runtime"
    return "other"


def _diff_stats(root: Path, base: str) -> tuple[dict[str, tuple[int, int]], set[str]]:
    stats: dict[str, tuple[int, int]] = {}
    for line in _git(root, "diff", "--numstat", base, "--").splitlines():
        added, deleted, path = line.split("\t", maxsplit=2)
        stats[path.replace("\\", "/")] = (
            int(added) if added.isdigit() else 0,
            int(deleted) if deleted.isdigit() else 0,
        )
    untracked = {
        path.replace("\\", "/")
        for path in _git(root, "ls-files", "--others", "--exclude-standard").splitlines()
        if path.strip()
    }
    for path in untracked:
        file_path = root / path
        try:
            added = len(file_path.read_text(encoding="utf-8").splitlines())
        except (OSError, UnicodeDecodeError):
            added = 0
        stats[path] = (added, 0)
    return stats, untracked


def _added_source(root: Path, base: str, paths: set[str], untracked: set[str]) -> str:
    tracked = sorted(paths - untracked)
    chunks: list[str] = []
    if tracked:
        chunks.extend(
            line[1:]
            for line in _git(
                root,
                "diff",
                "-U0",
                "--no-ext-diff",
                base,
                "--",
                *tracked,
            ).splitlines()
            if line.startswith("+") and not line.startswith("+++")
        )
    for path in sorted(untracked & paths):
        try:
            chunks.append((root / path).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue
    return "\n".join(chunks)


def _base_registry_has_guard(root: Path, base: str) -> bool:
    raw = _git(
        root,
        "show",
        f"{base}:docs/governance/current_work_packages.json",
        check=False,
    )
    if not raw:
        return False
    try:
        return json.loads(raw).get("scope_guard_version") == "scope-guard-v1"
    except json.JSONDecodeError:
        return False


def _registry_policy_changed(root: Path, base: str) -> bool:
    raw = _git(
        root,
        "show",
        f"{base}:docs/governance/current_work_packages.json",
        check=False,
    )
    try:
        before = json.loads(raw)
        after = json.loads(
            (root / "docs/governance/current_work_packages.json").read_text(
                encoding="utf-8"
            )
        )
    except (OSError, json.JSONDecodeError):
        return True
    before.pop("active_slice", None)
    after.pop("active_slice", None)
    return before != after


def _freeze_marker_only(
    root: Path,
    *,
    freeze_parent_commit: str,
    formal_candidate_commit: str,
) -> bool:
    observed_parent = _git(
        root,
        "rev-parse",
        f"{formal_candidate_commit}^",
        check=False,
    )
    if observed_parent != freeze_parent_commit:
        return False
    registry_path = "docs/governance/current_work_packages.json"
    changed_paths = {
        path.replace("\\", "/")
        for path in _git(
            root,
            "diff",
            "--name-only",
            freeze_parent_commit,
            formal_candidate_commit,
            "--",
        ).splitlines()
        if path.strip()
    }
    if changed_paths != {registry_path}:
        return False
    try:
        before = json.loads(
            _git(root, "show", f"{freeze_parent_commit}:{registry_path}")
        )
        after = json.loads(
            _git(root, "show", f"{formal_candidate_commit}:{registry_path}")
        )
        before_slice = before["active_slice"]
        after_slice = after["active_slice"]
    except (KeyError, json.JSONDecodeError, ScopeGuardError):
        return False
    if before_slice.get("phase") != "PREFLIGHT":
        return False
    if after_slice.get("phase") not in {"EVIDENCE_FROZEN", "GATE_RUNNING"}:
        return False
    if after_slice.get("freeze_parent_commit") != freeze_parent_commit:
        return False
    after_slice["phase"] = before_slice.get("phase")
    if "freeze_parent_commit" in before_slice:
        after_slice["freeze_parent_commit"] = before_slice["freeze_parent_commit"]
    else:
        after_slice.pop("freeze_parent_commit", None)
    return before == after


def _preflight_findings(root: Path, active_slice: ActiveSlice) -> list[str]:
    findings: list[str] = []
    for relative in active_slice.preflight_entrypoints:
        path = root / relative
        if not path.is_file():
            findings.append(f"missing_entrypoint:{relative}")
            continue
        if path.suffix != ".py":
            continue
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=relative)
        except (OSError, SyntaxError, UnicodeDecodeError):
            findings.append(f"invalid_entrypoint:{relative}")
            continue
        declared: set[str] = set()
        consumed: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr == "add_argument" and node.args:
                    first = node.args[0]
                    if isinstance(first, ast.Constant) and isinstance(first.value, str):
                        if first.value.startswith("--"):
                            declared.add(first.value[2:].replace("-", "_"))
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                if node.value.id == "args":
                    consumed.add(node.attr)
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                if (
                    _PASS_FIELD_PATTERN.search(node.target.id)
                    and isinstance(node.value, ast.Constant)
                    and node.value.value is True
                ):
                    findings.append(f"unsafe_true_default:{relative}:{node.target.id}")
        for name in sorted(declared - consumed):
            findings.append(f"unconsumed_cli_argument:{relative}:--{name.replace('_', '-')}")
        for token in active_slice.preflight_required_tokens.get(relative, []):
            if token not in source:
                findings.append(f"missing_required_readback:{relative}:{token}")
    return findings


def _receipt_progress(paths: list[Path], root: Path, valid_commits: set[str]) -> set[str]:
    progress: set[str] = set()
    resolved_root = root.resolve()
    for path in paths:
        resolved = path.resolve(strict=True)
        if resolved == resolved_root or resolved_root in resolved.parents:
            raise ScopeGuardError("scope evidence receipt must stay outside the repository")
        payload = json.loads(resolved.read_text(encoding="utf-8"))
        if payload.get("candidate_commit") not in valid_commits:
            raise ScopeGuardError("scope evidence receipt candidate binding differs")
        if payload.get("evidence_level") == "LIVE_PROVIDER_EVIDENCE":
            progress.add("PROVIDER")
        if "aggregate_metrics" in payload or "metrics" in payload:
            progress.add("EVAL_METRIC")
    return progress


def validate_mainline_scope(
    policy_root: Path,
    *,
    target_root: Path | None = None,
    requested_phase: ScopePhase | None = None,
    expected_candidate_commit: str | None = None,
    evidence_receipts: list[Path] | None = None,
) -> ScopeGuardReport:
    registry = WorkPackageRegistry.model_validate_json(
        (policy_root / "docs/governance/current_work_packages.json").read_bytes()
    )
    if registry.scope_guard_version != "scope-guard-v1" or registry.active_slice is None:
        raise ScopeGuardError("scope guard is not installed in the active registry")
    active = registry.active_slice
    target = (target_root or policy_root).resolve()
    audit_mode = target != policy_root.resolve()
    head = _git(target, "rev-parse", "HEAD")
    stats, untracked = _diff_stats(target, active.base_commit)
    changed = set(stats)
    classifications: dict[str, list[str]] = {}
    for path in sorted(changed):
        classifications.setdefault(_classify(path), []).append(path)

    errors: set[ScopeGuardErrorCode] = set()
    mechanisms: set[str] = set()
    if registry.scope_policy_sha256 != scope_policy_digest(policy_root):
        errors.add("POLICY_SELF_MODIFICATION")
        mechanisms.add("scope_policy_hash_mismatch")
    outside = sorted(
        path
        for path in changed
        if not any(_contains(allowed, path) for allowed in active.allowed_paths)
    )
    if outside and not audit_mode:
        errors.add("STAGE_SCOPE_VIOLATION")
        mechanisms.add("outside_allowed_paths")

    guard_preexisted = _base_registry_has_guard(target, active.base_commit)
    if guard_preexisted and not audit_mode:
        policy_changed = bool((changed & POLICY_PATHS) - {"docs/governance/current_work_packages.json"})
        if "docs/governance/current_work_packages.json" in changed:
            policy_changed = policy_changed or _registry_policy_changed(target, active.base_commit)
        registered_policy_fix = (
            active.work_kind == "CURRENT_GATE_FIX"
            and bool(changed & POLICY_PATHS)
            and all(
                any(_contains(allowed, path) for allowed in active.allowed_paths)
                for path in changed & POLICY_PATHS
            )
            and (changed & POLICY_PATHS).issubset(CURRENT_GATE_POLICY_FIX_PATHS)
        )
        if policy_changed and not registered_policy_fix:
            errors.add("POLICY_SELF_MODIFICATION")

    executable_paths = {
        path
        for path in changed
        if path != "backend/evals/agent_gate_v1/scope_guard.py"
        and _classify(path) != "tests"
        and (
            Path(path).suffix in {".py", ".sql", ".toml"}
            or path in DEPENDENCY_PATHS
        )
    }
    added_source = _added_source(target, active.base_commit, executable_paths, untracked)
    hardening_path = any(
        path in executable_paths and _HARDENING_PATH_PATTERN.search(path)
        for path in changed
    )
    durable = bool(_DURABLE_PATTERN.search(added_source) or hardening_path)
    crypto = bool(_CRYPTO_PATTERN.search(added_source))
    migration = any("/migrations/" in f"/{path}" for path in changed)
    dependency = bool(changed & DEPENDENCY_PATHS)
    if durable:
        mechanisms.add("durable_eval_state_or_custody")
    if crypto:
        mechanisms.add("cryptographic_or_signature_protocol")
    restricted_mechanism = registry.active_goal_sequence < 7
    if durable and restricted_mechanism:
        errors.add("NEW_DURABLE_EVAL_STATE")
    if crypto and restricted_mechanism:
        errors.add("NEW_CRYPTO_PROTOCOL")
    if migration:
        mechanisms.add("migration")
    if dependency:
        mechanisms.add("dependency_change")

    preflight_phase = active.phase in {"PREFLIGHT", "EVIDENCE_FROZEN", "GATE_RUNNING"}
    preflight_phase = preflight_phase or requested_phase in {
        "PREFLIGHT",
        "EVIDENCE_FROZEN",
        "GATE_RUNNING",
    }
    preflight = (
        [] if audit_mode or not preflight_phase else _preflight_findings(target, active)
    )
    if preflight:
        errors.add("STAGE_SCOPE_VIOLATION")
        mechanisms.update(preflight)
    frozen_gate_entry = (
        active.phase == "EVIDENCE_FROZEN" and requested_phase == "GATE_RUNNING"
    )
    if (
        requested_phase is not None
        and requested_phase != active.phase
        and not frozen_gate_entry
    ):
        errors.add("STAGE_SCOPE_VIOLATION")
        mechanisms.add("requested_phase_differs")

    if active.phase in {"EVIDENCE_FROZEN", "GATE_RUNNING"}:
        assert active.freeze_parent_commit is not None
        formal_candidate_commit = head
        if expected_candidate_commit and formal_candidate_commit != expected_candidate_commit:
            errors.add("EVIDENCE_FREEZE_BROKEN")
        if not _freeze_marker_only(
            target,
            freeze_parent_commit=active.freeze_parent_commit,
            formal_candidate_commit=formal_candidate_commit,
        ):
            errors.add("EVIDENCE_FREEZE_BROKEN")
        if _git(target, "status", "--porcelain"):
            errors.add("EVIDENCE_FREEZE_BROKEN")

    non_generated = [path for path in changed if not _is_generated(path)]
    handwritten_lines = sum(
        added
        for path, (added, _deleted) in stats.items()
        if not _is_generated(path) and Path(path).suffix in _HANDWRITTEN_CODE_SUFFIXES
    )
    new_schemas = sum(
        1
        for path in changed
        if path.endswith(".schema.json")
        and not _git_succeeds(target, "cat-file", "-e", f"{active.base_commit}:{path}")
    )
    budget_exceeded = (
        active.work_kind in {"CURRENT_GATE_FIX", "EVAL_INFRA"}
        and (guard_preexisted or audit_mode)
        and (
            len(non_generated) > MAX_NON_GENERATED_FILES
            or handwritten_lines > MAX_HANDWRITTEN_ADDED_LINES
            or new_schemas > MAX_NEW_SCHEMAS
        )
    )
    if budget_exceeded:
        errors.add("BUDGET_EXCEEDED")

    product_progress: set[str] = set()
    if active.work_kind != "EVAL_INFRA":
        if classifications.get("ui_runtime"):
            product_progress.add("UI")
        if classifications.get("api_runtime"):
            product_progress.add("API")
        if classifications.get("product_runtime"):
            product_progress.add("RUNTIME")
        valid_commits = {head, active.base_commit}
        product_progress |= _receipt_progress(
            evidence_receipts or [], target, valid_commits
        )

    runtime_changed = any(
        classifications.get(kind)
        for kind in ("ui_runtime", "api_runtime", "product_runtime")
    )
    meaningful_changed = changed - {"docs/governance/current_work_packages.json"}
    if meaningful_changed and active.work_kind == "PRODUCT" and not product_progress:
        errors.add("STAGE_SCOPE_VIOLATION")
        mechanisms.add("product_slice_without_product_progress")
    if active.work_kind == "EVAL_INFRA" and runtime_changed:
        errors.add("STAGE_SCOPE_VIOLATION")
        mechanisms.add("eval_infra_changed_product_runtime")

    early_hardening = registry.active_goal_sequence < 7 and (
        active.work_kind == "HARDENING"
        or durable
        or crypto
        or migration
        or dependency
    )
    if "POLICY_SELF_MODIFICATION" in errors or "EVIDENCE_FREEZE_BROKEN" in errors:
        verdict: ScopeGuardVerdict = "REJECT"
        action = "Restore the frozen policy or candidate before continuing."
    elif "STAGE_SCOPE_VIOLATION" in errors:
        verdict = "REJECT"
        action = "Narrow the slice to its declared paths and phase."
    elif early_hardening:
        verdict = "DEFER_TO_G07"
        action = "Remove the new hardening mechanism or record it for G07."
    elif budget_exceeded:
        verdict = "SCOPE_REVIEW_REQUIRED"
        action = "Reduce, split or defer the slice; the Agent cannot raise its budget."
    else:
        verdict = "PASS"
        action = "Continue within the declared slice and rerun before commit."

    invalidated = set(active.evidence_invalidated)
    if classifications.get("eval_infra"):
        invalidated.update({"EVAL_SCORE", "FINAL_GATE"})
    if classifications.get("product_runtime") or classifications.get("api_runtime"):
        invalidated.update({"RUNTIME", "PROVIDER", "E2E", "FINAL_GATE"})
    if classifications.get("ui_runtime"):
        invalidated.update({"UI", "E2E", "FINAL_GATE"})
    if classifications.get("governance"):
        invalidated.add("GATE_BINDING")

    return ScopeGuardReport(
        mode="AUDIT" if audit_mode else "ENFORCE",
        slice_id=active.slice_id,
        base_commit=active.base_commit,
        observed_head=head,
        declared_phase=active.phase,
        requested_phase=requested_phase,
        work_kind=active.work_kind,
        verdict=verdict,
        error_codes=sorted(errors),
        changed_files=sorted(changed),
        non_generated_file_count=len(non_generated),
        handwritten_added_lines=handwritten_lines,
        new_schema_count=new_schemas,
        classifications={key: value for key, value in sorted(classifications.items())},
        detected_mechanisms=sorted(mechanisms),
        product_progress=sorted(product_progress) or ["NONE"],
        invalidated_evidence=sorted(invalidated),
        required_action=action,
    )
