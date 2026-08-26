from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import secrets
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping


SCHEMA_VERSION = "dual-entry-run-spec-v1"
AUTO_RESOLVE = "RESOLVE_AT_RUN_START"
REQUIRED_PLACEHOLDER = "REQUIRED_AT_RUN_START"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SQL_RE = re.compile(r"(?:\bsql[_ -]?seed\b|\binsert\s+into\b|\bcopy\s+[^\s]+\s+from\b)", re.IGNORECASE)
_BLIND_VALUE_RE = re.compile(r"(?:frozen_blind\.labels|sealed[/\\].*labels|\.labels\.jsonl)", re.IGNORECASE)
_BLIND_ORACLE_KEYS = {
    "deterministic_truth",
    "expected",
    "expected_answer",
    "expected_candidate_order",
    "expected_findings",
    "gate_assertions",
    "human_label",
    "judge_scores",
    "must_be_unknown",
    "must_fail",
    "must_not_happen",
    "must_pass",
    "oracle",
}
_TOP_LEVEL_REQUIRED = {
    "schema_version",
    "lane",
    "purpose",
    "sut",
    "dataset",
    "provider",
    "models",
    "comparison",
    "execution",
    "budget",
    "thresholds",
    "artifacts",
    "prohibitions",
}
_CHECK_IDS = (
    "RUN_SPEC_CONTRACT",
    "REQUIRED_BINDINGS",
    "DATASET_BINDING",
    "SOURCE_BINDING",
    "MODEL_BINDING",
    "BLIND_ISOLATION",
    "SQL_SEED_PROHIBITION",
    "PROVIDER_MODE_ISOLATION",
    "PROVIDER_SNAPSHOT_BINDING",
    "GRADED_RANKING_ORACLE_BINDING",
)
_RUNTIME_ENV_KEYS = (
    "AMAP_MOCK",
    "DEMO_MODE",
    "BREEZETRAVEL_PROVIDER_MODE",
    "EVAL_PRODUCT_ADAPTER",
)


@dataclass(frozen=True)
class PreflightResult:
    spec_path: Path
    repo_root: Path
    source_spec_sha256: str | None
    resolved_spec: dict[str, Any] | None
    bindings: dict[str, Any]
    checks: tuple[dict[str, Any], ...]
    errors: tuple[dict[str, Any], ...]

    @property
    def valid(self) -> bool:
        return not self.errors and self.resolved_spec is not None

    def summary(self) -> dict[str, Any]:
        return {
            "schema_version": "continuous-preflight-v1",
            "spec_path": str(self.spec_path),
            "status": "VALID" if self.valid else "INVALID",
            "decision": "ACCEPT_PREFLIGHT" if self.valid else "REJECT",
            "source_spec_sha256": self.source_spec_sha256,
            "bindings": self.bindings,
            "checks": list(self.checks),
            "errors": list(self.errors),
        }


@dataclass(frozen=True)
class RunResult:
    run_id: str
    run_dir: Path
    gate: dict[str, Any]

    def summary(self) -> dict[str, Any]:
        return {
            "schema_version": "continuous-run-result-v1",
            "run_id": self.run_id,
            "run_dir": str(self.run_dir),
            "status": self.gate["status"],
            "decision": self.gate["decision"],
            "reason": self.gate["execution"]["reason"],
        }


class _Validation:
    def __init__(self) -> None:
        self.failures: list[dict[str, Any]] = []
        self.failed_checks: set[str] = set()

    def fail(self, check: str, code: str, message: str, path: str | None = None) -> None:
        self.failed_checks.add(check)
        item: dict[str, Any] = {"code": code, "message": message}
        if path:
            item["path"] = path
        self.failures.append(item)

    def checks(self) -> tuple[dict[str, Any], ...]:
        return tuple(
            {"id": check, "status": "FAIL" if check in self.failed_checks else "PASS"} for check in _CHECK_IDS
        )


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256_json(value: Any) -> str:
    return _sha256_bytes(_canonical_bytes(value))


def _read_jsonl(path: Path, validation: _Validation, check: str, code_prefix: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        validation.fail(check, f"{code_prefix}_READ_FAILED", f"Cannot read {path}: {exc}", str(path))
        return rows
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            validation.fail(
                check,
                f"{code_prefix}_JSON_INVALID",
                f"Invalid JSONL at line {line_number}: {exc.msg}",
                f"{path}:{line_number}",
            )
            continue
        if not isinstance(row, dict):
            validation.fail(
                check,
                f"{code_prefix}_ROW_INVALID",
                f"JSONL row {line_number} must be an object",
                f"{path}:{line_number}",
            )
            continue
        rows.append(row)
    return rows


def _walk(value: Any, path: str = "$") -> Iterable[tuple[str, str | None, Any]]:
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            yield child_path, str(key), child
            yield from _walk(child, child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            child_path = f"{path}[{index}]"
            yield child_path, None, child
            yield from _walk(child, child_path)


def _resolve_inside(root: Path, raw_path: str, validation: _Validation, check: str, code: str) -> Path | None:
    candidate = Path(raw_path)
    resolved = (candidate if candidate.is_absolute() else root / candidate).resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError:
        validation.fail(check, code, f"Path escapes repository root: {raw_path}", raw_path)
        return None
    return resolved


def _run_git(repo_root: Path, *args: str) -> bytes:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo_root,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return completed.stdout


def _discover_repo_root(start: Path) -> Path:
    try:
        output = _run_git(start, "rev-parse", "--show-toplevel")
    except (OSError, subprocess.CalledProcessError):
        return start.resolve()
    return Path(output.decode("utf-8", errors="surrogateescape").strip()).resolve()


def _git_bindings(repo_root: Path, validation: _Validation) -> tuple[str | None, str | None]:
    try:
        commit = _run_git(repo_root, "rev-parse", "HEAD").decode("ascii").strip().lower()
        tracked_diff = _run_git(
            repo_root,
            "diff",
            "--binary",
            "--no-ext-diff",
            "HEAD",
            "--",
            ".",
            ":(exclude)backend/evidence/runs/**",
        )
        untracked_raw = _run_git(
            repo_root,
            "ls-files",
            "--others",
            "--exclude-standard",
            "-z",
            "--",
            ".",
            ":(exclude)backend/evidence/runs/**",
        )
    except (OSError, UnicodeError, subprocess.CalledProcessError) as exc:
        validation.fail("REQUIRED_BINDINGS", "GIT_BINDING_FAILED", f"Cannot bind current Git state: {exc}")
        return None, None

    digest = hashlib.sha256()
    digest.update(b"continuous-dirty-diff-v1\0")
    digest.update(len(tracked_diff).to_bytes(8, "big"))
    digest.update(tracked_diff)
    untracked_paths = sorted(path for path in untracked_raw.split(b"\0") if path)
    for raw_path in untracked_paths:
        relative = raw_path.decode("utf-8", errors="surrogateescape")
        file_path = repo_root / relative
        if not file_path.is_file():
            continue
        try:
            content_hash = _sha256_bytes(file_path.read_bytes()).encode("ascii")
        except OSError as exc:
            validation.fail(
                "REQUIRED_BINDINGS",
                "UNTRACKED_FILE_HASH_FAILED",
                f"Cannot hash untracked file {relative}: {exc}",
                relative,
            )
            continue
        digest.update(len(raw_path).to_bytes(8, "big"))
        digest.update(raw_path)
        digest.update(content_hash)
    return commit, digest.hexdigest()


def _runtime_config_hash(spec: Mapping[str, Any], environ: Mapping[str, str]) -> str:
    payload = copy.deepcopy(dict(spec))
    sut = payload.get("sut")
    if isinstance(sut, dict):
        for key in ("commit_sha", "dirty_diff_sha256", "runtime_config_sha256", "docker_image_digest"):
            sut.pop(key, None)
    dataset = payload.get("dataset")
    if isinstance(dataset, dict):
        dataset.pop("manifest_sha256", None)
        dataset.pop("case_ids_sha256", None)
    execution = payload.get("execution")
    if isinstance(execution, dict):
        execution.pop("cache_namespace_sha256", None)
        execution.pop("bindings", None)
    payload["runtime_environment"] = {key: environ.get(key) for key in _RUNTIME_ENV_KEYS}
    return _sha256_json(payload)


def _effective_runtime_env(repo_root: Path, environ: Mapping[str, str] | None) -> dict[str, str]:
    if environ is not None:
        return {key: value for key in _RUNTIME_ENV_KEYS if (value := environ.get(key)) is not None}

    effective: dict[str, str] = {}
    dotenv = repo_root / ".env"
    try:
        lines = dotenv.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key in _RUNTIME_ENV_KEYS:
            effective[key] = value.strip().strip('"').strip("'")
    for key in _RUNTIME_ENV_KEYS:
        if key in os.environ:
            effective[key] = os.environ[key]
    return effective


def _bind_or_verify(
    container: dict[str, Any],
    key: str,
    actual: str | None,
    validation: _Validation,
    path: str,
) -> None:
    if actual is None:
        return
    declared = container.get(key)
    if declared == AUTO_RESOLVE:
        container[key] = actual
    elif isinstance(declared, str) and (declared == REQUIRED_PLACEHOLDER or declared.startswith("REQUIRED_")):
        # The dedicated placeholder check reports the actionable failure.  Do
        # not also misclassify an unresolved binding as a stale concrete hash.
        return
    elif declared != actual:
        validation.fail(
            "REQUIRED_BINDINGS",
            "STALE_OR_MISMATCHED_BINDING",
            f"Declared binding does not match current value at {path}",
            path,
        )


def _validate_top_level(spec: Any, validation: _Validation) -> bool:
    if not isinstance(spec, dict):
        validation.fail("RUN_SPEC_CONTRACT", "RUN_SPEC_NOT_OBJECT", "RunSpec root must be a JSON object")
        return False
    missing = sorted(_TOP_LEVEL_REQUIRED - set(spec))
    if missing:
        validation.fail(
            "RUN_SPEC_CONTRACT",
            "RUN_SPEC_REQUIRED_FIELDS_MISSING",
            f"Missing required top-level fields: {', '.join(missing)}",
        )
    if spec.get("schema_version") != SCHEMA_VERSION:
        validation.fail(
            "RUN_SPEC_CONTRACT",
            "RUN_SPEC_SCHEMA_VERSION_UNSUPPORTED",
            f"schema_version must be {SCHEMA_VERSION}",
            "$.schema_version",
        )
    for field in ("sut", "dataset", "provider", "models", "comparison", "execution", "budget", "thresholds"):
        if field in spec and not isinstance(spec[field], dict):
            validation.fail("RUN_SPEC_CONTRACT", "RUN_SPEC_FIELD_TYPE_INVALID", f"{field} must be an object", f"$.{field}")
    for field in ("artifacts", "prohibitions"):
        if field in spec and not isinstance(spec[field], list):
            validation.fail("RUN_SPEC_CONTRACT", "RUN_SPEC_FIELD_TYPE_INVALID", f"{field} must be an array", f"$.{field}")
    return not missing


def _validate_required_placeholders(spec: Mapping[str, Any], validation: _Validation) -> None:
    for path, _key, value in _walk(spec):
        if isinstance(value, str) and (value == REQUIRED_PLACEHOLDER or value.startswith("REQUIRED_")):
            validation.fail(
                "REQUIRED_BINDINGS",
                "UNRESOLVED_REQUIRED_PLACEHOLDER",
                "A REQUIRED placeholder must be injected before the run starts",
                path,
            )


def _validate_no_auto_placeholders_remain(spec: Mapping[str, Any], validation: _Validation) -> None:
    for path, _key, value in _walk(spec):
        if value == AUTO_RESOLVE:
            validation.fail(
                "REQUIRED_BINDINGS",
                "UNRESOLVED_AUTO_PLACEHOLDER",
                "RESOLVE_AT_RUN_START is only valid for runner-owned binding fields",
                path,
            )


def _validate_models(spec: Mapping[str, Any], validation: _Validation) -> None:
    models = spec.get("models")
    if not isinstance(models, dict):
        return
    for name, config in models.items():
        path = f"$.models.{name}"
        if not isinstance(config, dict):
            validation.fail("MODEL_BINDING", "MODEL_CONFIG_INVALID", f"Model config {name} must be an object", path)
            continue
        enabled = config.get("enabled")
        if not isinstance(enabled, bool):
            validation.fail("MODEL_BINDING", "MODEL_ENABLED_FLAG_MISSING", f"Model {name} needs a boolean enabled flag", path)
            continue
        if enabled:
            for field in ("provider", "model"):
                if not isinstance(config.get(field), str) or not config[field] or config[field] == "disabled":
                    validation.fail(
                        "MODEL_BINDING",
                        "ENABLED_MODEL_BINDING_MISSING",
                        f"Enabled model {name} is missing {field}",
                        f"{path}.{field}",
                    )
            prompt_hash = config.get("prompt_sha256")
            if not isinstance(prompt_hash, str) or not _SHA256_RE.fullmatch(prompt_hash):
                validation.fail(
                    "MODEL_BINDING",
                    "MODEL_PROMPT_HASH_INVALID",
                    f"Enabled model {name} needs a concrete SHA-256 prompt hash",
                    f"{path}.prompt_sha256",
                )
        elif config.get("model") != "disabled":
            validation.fail(
                "MODEL_BINDING",
                "DISABLED_MODEL_NOT_EXPLICIT",
                f"Disabled model {name} must use model='disabled'",
                f"{path}.model",
            )


def _validate_sql_seed(spec: Mapping[str, Any], cases: Iterable[Mapping[str, Any]], validation: _Validation) -> None:
    sut = spec.get("sut") if isinstance(spec.get("sut"), dict) else {}
    if sut.get("allow_sql_seed") is not False:
        validation.fail(
            "SQL_SEED_PROHIBITION",
            "SQL_SEED_NOT_DISABLED",
            "sut.allow_sql_seed must be false",
            "$.sut.allow_sql_seed",
        )
    prohibitions = spec.get("prohibitions") if isinstance(spec.get("prohibitions"), list) else []
    if "sql_seed" not in prohibitions:
        validation.fail(
            "SQL_SEED_PROHIBITION",
            "SQL_SEED_PROHIBITION_MISSING",
            "RunSpec prohibitions must include sql_seed",
            "$.prohibitions",
        )

    for section_name in ("sut", "execution"):
        section = spec.get(section_name)
        if not isinstance(section, dict):
            continue
        for path, key, value in _walk(section, f"$.{section_name}"):
            if key == "allow_sql_seed" and value is False:
                continue
            if (key and _SQL_RE.search(key)) or (isinstance(value, str) and _SQL_RE.search(value)):
                validation.fail(
                    "SQL_SEED_PROHIBITION",
                    "SQL_SEED_EXECUTION_CONFIGURED",
                    "Executable RunSpec configuration contains a SQL seed operation",
                    path,
                )

    for case in cases:
        execution = case.get("execution")
        if not isinstance(execution, dict):
            continue
        for path, key, value in _walk(execution, f"case:{case.get('case_id', '<unknown>')}.execution"):
            if (key and _SQL_RE.search(key)) or (isinstance(value, str) and _SQL_RE.search(value)):
                validation.fail(
                    "SQL_SEED_PROHIBITION",
                    "CASE_SQL_SEED_STEP_FOUND",
                    "A selected case contains a SQL seed execution step",
                    path,
                )


def _truthy_env(value: str | None) -> bool:
    return bool(value and value.strip().lower() in {"1", "true", "yes", "on"})


def _validate_frozen_snapshot_artifact(
    spec: Mapping[str, Any],
    repo_root: Path,
    validation: _Validation,
) -> dict[str, Any]:
    """Bind a frozen-provider RunSpec to one checked-in, byte-exact artifact."""
    provider = spec.get("provider") if isinstance(spec.get("provider"), dict) else {}
    if provider.get("mode") != "frozen_snapshot":
        return {}

    raw_path = provider.get("snapshot_path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        validation.fail(
            "PROVIDER_SNAPSHOT_BINDING",
            "SNAPSHOT_PATH_MISSING",
            "Frozen snapshot mode requires a repository-relative snapshot_path",
            "$.provider.snapshot_path",
        )
        return {}
    if Path(raw_path).is_absolute():
        validation.fail(
            "PROVIDER_SNAPSHOT_BINDING",
            "SNAPSHOT_PATH_NOT_REPOSITORY_RELATIVE",
            "snapshot_path must be relative to the repository root",
            "$.provider.snapshot_path",
        )
        return {}
    artifact_path = _resolve_inside(
        repo_root,
        raw_path,
        validation,
        "PROVIDER_SNAPSHOT_BINDING",
        "SNAPSHOT_PATH_OUTSIDE_REPOSITORY",
    )
    if artifact_path is None:
        return {}
    if not artifact_path.is_file():
        validation.fail(
            "PROVIDER_SNAPSHOT_BINDING",
            "SNAPSHOT_ARTIFACT_MISSING",
            "Frozen snapshot artifact does not exist or is not a file",
            "$.provider.snapshot_path",
        )
        return {}

    try:
        artifact_bytes = artifact_path.read_bytes()
    except OSError as exc:
        validation.fail(
            "PROVIDER_SNAPSHOT_BINDING",
            "SNAPSHOT_ARTIFACT_READ_FAILED",
            f"Cannot read frozen snapshot artifact: {exc}",
            "$.provider.snapshot_path",
        )
        return {}
    artifact_file_sha256 = _sha256_bytes(artifact_bytes)
    if provider.get("snapshot_sha256") != artifact_file_sha256:
        validation.fail(
            "PROVIDER_SNAPSHOT_BINDING",
            "SNAPSHOT_ARTIFACT_SHA256_MISMATCH",
            "snapshot_sha256 does not match the snapshot artifact's exact bytes",
            "$.provider.snapshot_sha256",
        )

    try:
        artifact = json.loads(artifact_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        validation.fail(
            "PROVIDER_SNAPSHOT_BINDING",
            "SNAPSHOT_ARTIFACT_JSON_INVALID",
            f"Frozen snapshot artifact is not valid UTF-8 JSON: {exc}",
            "$.provider.snapshot_path",
        )
        return {"provider_snapshot_path": artifact_path.relative_to(repo_root).as_posix(),
                "provider_snapshot_file_sha256": artifact_file_sha256}
    if not isinstance(artifact, dict):
        validation.fail(
            "PROVIDER_SNAPSHOT_BINDING",
            "SNAPSHOT_ARTIFACT_NOT_OBJECT",
            "Frozen snapshot artifact root must be a JSON object",
            "$.provider.snapshot_path",
        )
        return {"provider_snapshot_path": artifact_path.relative_to(repo_root).as_posix(),
                "provider_snapshot_file_sha256": artifact_file_sha256}

    expected_class = "real_provider_local_authorized"
    snapshot_schema = artifact.get("schema_version")
    expected_subtype = {
        "1.0": "suggestion_live_candidate_and_walking_route_snapshot",
        "1.1": "suggestion_live_chained_candidate_and_walking_route_snapshot",
    }.get(snapshot_schema)
    if expected_subtype is None:
        validation.fail(
            "PROVIDER_SNAPSHOT_BINDING",
            "SNAPSHOT_SCHEMA_VERSION_UNSUPPORTED",
            "Frozen Suggestion snapshot schema_version must be 1.0 or 1.1",
            "$.provider.snapshot_path:schema_version",
        )
    if artifact.get("evidence_class") != expected_class:
        validation.fail(
            "PROVIDER_SNAPSHOT_BINDING",
            "SNAPSHOT_EVIDENCE_CLASS_INVALID",
            f"Frozen Suggestion snapshot evidence_class must be {expected_class}",
            "$.provider.snapshot_path:evidence_class",
        )
    if artifact.get("evidence_subtype") != expected_subtype:
        validation.fail(
            "PROVIDER_SNAPSHOT_BINDING",
            "SNAPSHOT_EVIDENCE_SUBTYPE_INVALID",
            f"Frozen Suggestion snapshot evidence_subtype must be {expected_subtype}",
            "$.provider.snapshot_path:evidence_subtype",
        )
    expected_status = "passed" if snapshot_schema == "1.0" else "PASSED"
    if artifact.get("overall_status") != expected_status:
        validation.fail(
            "PROVIDER_SNAPSHOT_BINDING",
            "SNAPSHOT_OVERALL_STATUS_NOT_PASSED",
            f"Frozen snapshot artifact overall_status must be {expected_status}",
            "$.provider.snapshot_path:overall_status",
        )

    integrity = artifact.get("integrity")
    payload_sha256: str | None = None
    if not isinstance(integrity, dict):
        validation.fail(
            "PROVIDER_SNAPSHOT_BINDING",
            "SNAPSHOT_INTEGRITY_MISSING",
            "Frozen snapshot artifact requires an integrity object",
            "$.provider.snapshot_path:integrity",
        )
    else:
        declared_payload_sha256 = integrity.get("artifact_payload_sha256")
        calculated_payload_sha256 = _sha256_json({key: value for key, value in artifact.items() if key != "integrity"})
        if (
            not isinstance(declared_payload_sha256, str)
            or not _SHA256_RE.fullmatch(declared_payload_sha256)
            or declared_payload_sha256 != calculated_payload_sha256
        ):
            validation.fail(
                "PROVIDER_SNAPSHOT_BINDING",
                "SNAPSHOT_PAYLOAD_INTEGRITY_MISMATCH",
                "integrity.artifact_payload_sha256 does not match canonical artifact content",
                "$.provider.snapshot_path:integrity.artifact_payload_sha256",
            )
        else:
            payload_sha256 = declared_payload_sha256
        if integrity.get("passed") is not True or integrity.get("validation_errors") != []:
            validation.fail(
                "PROVIDER_SNAPSHOT_BINDING",
                "SNAPSHOT_INTEGRITY_NOT_PASSED",
                "Frozen snapshot integrity must be passed with no validation errors",
                "$.provider.snapshot_path:integrity",
            )

    claim_boundary = artifact.get("claim_boundary")
    if not isinstance(claim_boundary, dict) or any(
        claim_boundary.get(field) is not False
        for field in (
            "proves_opening_hours",
            "is_public_internet_e2e",
            "is_human_evidence",
            "is_release_approval",
        )
    ):
        validation.fail(
            "PROVIDER_SNAPSHOT_BINDING",
            "SNAPSHOT_CLAIM_BOUNDARY_INVALID",
            "Local capture must explicitly deny opening, public E2E, human, and release claims",
            "$.provider.snapshot_path:claim_boundary",
        )

    if payload_sha256 is not None and provider.get("snapshot_id") != payload_sha256:
        validation.fail(
            "PROVIDER_SNAPSHOT_BINDING",
            "SNAPSHOT_ID_NOT_ARTIFACT_BOUND",
            "snapshot_id must equal integrity.artifact_payload_sha256",
            "$.provider.snapshot_id",
        )

    return {
        "provider_snapshot_path": artifact_path.relative_to(repo_root).as_posix(),
        "provider_snapshot_file_sha256": artifact_file_sha256,
        "provider_snapshot_payload_sha256": payload_sha256,
        "provider_snapshot_id": provider.get("snapshot_id"),
        "provider_snapshot_evidence_class": artifact.get("evidence_class"),
        "provider_snapshot_evidence_subtype": artifact.get("evidence_subtype"),
        "provider_snapshot_overall_status": artifact.get("overall_status"),
    }


def _validate_provider_mode(
    spec: Mapping[str, Any],
    cases: Iterable[Mapping[str, Any]],
    environ: Mapping[str, str],
    validation: _Validation,
) -> None:
    provider = spec.get("provider") if isinstance(spec.get("provider"), dict) else {}
    mode = provider.get("mode")
    lane = spec.get("lane")
    expected_by_lane = {
        "pr_offline": "controlled_fixture",
        "nightly_snapshot": "frozen_snapshot",
        "weekly_live": "live_provider",
        "release_blind": "frozen_snapshot",
        "human_calibration": "human_only",
    }
    if mode not in {"controlled_fixture", "frozen_snapshot", "live_provider", "human_only"}:
        validation.fail("PROVIDER_MODE_ISOLATION", "PROVIDER_MODE_INVALID", "Provider mode is unsupported", "$.provider.mode")
        return
    if expected_by_lane.get(str(lane)) != mode:
        validation.fail(
            "PROVIDER_MODE_ISOLATION",
            "LANE_PROVIDER_MODE_MISMATCH",
            f"Lane {lane!r} requires provider mode {expected_by_lane.get(str(lane))!r}",
            "$.provider.mode",
        )
    if mode in {"frozen_snapshot", "live_provider"} and provider.get("fixture_fallback_allowed") is not False:
        validation.fail(
            "PROVIDER_MODE_ISOLATION",
            "FIXTURE_FALLBACK_NOT_DISABLED",
            f"{mode} cannot allow fixture fallback",
            "$.provider.fixture_fallback_allowed",
        )
    if mode == "frozen_snapshot":
        if not isinstance(provider.get("snapshot_id"), str) or not provider.get("snapshot_id"):
            validation.fail(
                "PROVIDER_MODE_ISOLATION",
                "SNAPSHOT_ID_MISSING",
                "Frozen snapshot mode requires a concrete snapshot_id",
                "$.provider.snapshot_id",
            )
        snapshot_hash = provider.get("snapshot_sha256")
        if not isinstance(snapshot_hash, str) or not _SHA256_RE.fullmatch(snapshot_hash):
            validation.fail(
                "PROVIDER_MODE_ISOLATION",
                "SNAPSHOT_HASH_INVALID",
                "Frozen snapshot mode requires a concrete SHA-256 snapshot hash",
                "$.provider.snapshot_sha256",
            )
    if mode == "live_provider":
        if any(provider.get(field) is not None for field in ("snapshot_id", "snapshot_sha256", "snapshot_path")):
            validation.fail(
                "PROVIDER_MODE_ISOLATION",
                "LIVE_SNAPSHOT_MIXED",
                "Live provider mode cannot carry frozen snapshot identity",
                "$.provider",
            )
        if _truthy_env(environ.get("AMAP_MOCK")) or _truthy_env(environ.get("DEMO_MODE")):
            validation.fail(
                "PROVIDER_MODE_ISOLATION",
                "LIVE_RUNTIME_IS_MOCKED",
                "Live provider lane cannot run with AMAP_MOCK or DEMO_MODE enabled",
                "runtime_environment",
            )
    if mode == "controlled_fixture":
        if any(provider.get(field) is not None for field in ("snapshot_id", "snapshot_sha256", "snapshot_path")):
            validation.fail(
                "PROVIDER_MODE_ISOLATION",
                "FIXTURE_SNAPSHOT_MIXED",
                "Controlled fixture mode cannot carry frozen snapshot identity",
                "$.provider",
            )
        budget = spec.get("budget") if isinstance(spec.get("budget"), dict) else {}
        if budget.get("paid_api_allowed") is not False:
            validation.fail(
                "PROVIDER_MODE_ISOLATION",
                "FIXTURE_LANE_ALLOWS_PAID_API",
                "Controlled fixture lane must disable paid API calls",
                "$.budget.paid_api_allowed",
            )

    allowed_case_modes = {mode}
    case_modes = {
        execution.get("provider_mode")
        for case in cases
        if isinstance((execution := case.get("execution")), dict)
    }
    mismatches = sorted(str(item) for item in case_modes - allowed_case_modes)
    if mismatches:
        validation.fail(
            "PROVIDER_MODE_ISOLATION",
            "CASE_PROVIDER_MODE_MISMATCH",
            f"Selected cases require provider modes not supplied by this RunSpec: {', '.join(mismatches)}",
            "$.dataset.splits",
        )

    import_contract = provider.get("import_snapshot_contract")
    if import_contract is not None:
        expected_contract = {
            "schema_version": "import-frozen-provider-contract-v1",
            "runtime_adapter_id": "import-frozen-entity-route-weather-v1",
            "required_fact_types": ["ENTITY_RESOLUTION", "ROUTE_TIME", "WEATHER"],
        }
        if not isinstance(import_contract, dict) or any(
            import_contract.get(key) != value for key, value in expected_contract.items()
        ):
            validation.fail(
                "PROVIDER_SNAPSHOT_BINDING",
                "IMPORT_SNAPSHOT_CONTRACT_INVALID",
                "Import frozen snapshot must bind entity, route and weather through the versioned adapter contract",
                "$.provider.import_snapshot_contract",
            )
        else:
            if import_contract.get("status") != "READY":
                validation.fail(
                    "PROVIDER_SNAPSHOT_BINDING",
                    "IMPORT_SNAPSHOT_ARTIFACT_NOT_PROVISIONED",
                    "Import frozen entity/route/weather artifact is not provisioned",
                    "$.provider.import_snapshot_contract.status",
                )
            adapter_id = import_contract["runtime_adapter_id"]
            if environ.get("EVAL_PRODUCT_ADAPTER") != adapter_id:
                validation.fail(
                    "PROVIDER_SNAPSHOT_BINDING",
                    "IMPORT_SNAPSHOT_ADAPTER_NOT_ACTIVE",
                    f"EVAL_PRODUCT_ADAPTER must equal {adapter_id}",
                    "runtime_environment.EVAL_PRODUCT_ADAPTER",
                )


def _validate_blind_isolation(spec: Mapping[str, Any], cases: Iterable[Mapping[str, Any]], validation: _Validation) -> None:
    dataset = spec.get("dataset") if isinstance(spec.get("dataset"), dict) else {}
    splits = dataset.get("splits") if isinstance(dataset.get("splits"), list) else []
    if "frozen_blind" not in splits:
        return
    if dataset.get("label_access") != "isolated_scorer_only":
        validation.fail(
            "BLIND_ISOLATION",
            "BLIND_LABEL_ACCESS_NOT_ISOLATED",
            "frozen_blind requires isolated_scorer_only label access",
            "$.dataset.label_access",
        )
    judge = spec.get("models", {}).get("judge", {}) if isinstance(spec.get("models"), dict) else {}
    if isinstance(judge, dict) and judge.get("hidden_labels_allowed") is not False:
        validation.fail(
            "BLIND_ISOLATION",
            "JUDGE_LABEL_ACCESS_NOT_DISABLED",
            "Blind Judge must explicitly disable hidden-label access",
            "$.models.judge.hidden_labels_allowed",
        )
    prohibitions = set(spec.get("prohibitions", [])) if isinstance(spec.get("prohibitions"), list) else set()
    required = {
        "generator_access_to_labels",
        "sut_access_to_labels",
        "judge_access_to_labels",
        "repository_blind_label_payload",
        "in_process_blind_scoring",
    }
    missing = sorted(required - prohibitions)
    if missing:
        validation.fail(
            "BLIND_ISOLATION",
            "BLIND_PROHIBITIONS_INCOMPLETE",
            f"Blind RunSpec is missing prohibitions: {', '.join(missing)}",
            "$.prohibitions",
        )

    for path, key, value in _walk(spec):
        if path.startswith("$.prohibitions") or path in {
            "$.dataset.label_access",
            "$.models.judge.hidden_labels_allowed",
        }:
            continue
        lowered_key = key.lower() if key else ""
        if "label" in lowered_key or (isinstance(value, str) and _BLIND_VALUE_RE.search(value)):
            validation.fail(
                "BLIND_ISOLATION",
                "BLIND_LABEL_PATH_EXPOSED",
                "Blind RunSpec exposes a label-bearing field or path outside the isolated scorer",
                path,
            )

    for case in cases:
        for path, key, _value in _walk(case, f"case:{case.get('case_id', '<unknown>')}"):
            if key and key.lower() in _BLIND_ORACLE_KEYS:
                validation.fail(
                    "BLIND_ISOLATION",
                    "BLIND_ORACLE_IN_PRODUCT_INPUT",
                    "Blind product input contains an oracle or expected-answer field",
                    path,
                )


def _dataset_and_sources(
    spec: dict[str, Any],
    repo_root: Path,
    validation: _Validation,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    bindings: dict[str, Any] = {}
    dataset = spec.get("dataset")
    if not isinstance(dataset, dict):
        return [], bindings
    manifest_raw = dataset.get("manifest")
    if not isinstance(manifest_raw, str) or not manifest_raw:
        validation.fail("DATASET_BINDING", "DATASET_MANIFEST_PATH_MISSING", "dataset.manifest is required", "$.dataset.manifest")
        return [], bindings
    manifest_path = _resolve_inside(
        repo_root,
        manifest_raw,
        validation,
        "DATASET_BINDING",
        "DATASET_MANIFEST_PATH_OUTSIDE_REPO",
    )
    if manifest_path is None:
        return [], bindings
    try:
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes)
    except (OSError, json.JSONDecodeError) as exc:
        validation.fail("DATASET_BINDING", "DATASET_MANIFEST_INVALID", f"Cannot load dataset manifest: {exc}", str(manifest_path))
        return [], bindings
    if not isinstance(manifest, dict):
        validation.fail("DATASET_BINDING", "DATASET_MANIFEST_NOT_OBJECT", "Dataset manifest must be an object", str(manifest_path))
        return [], bindings

    manifest_hash = _sha256_bytes(manifest_bytes)
    _bind_or_verify(dataset, "manifest_sha256", manifest_hash, validation, "$.dataset.manifest_sha256")
    selected_splits = dataset.get("splits")
    if not isinstance(selected_splits, list) or not selected_splits or not all(isinstance(item, str) for item in selected_splits):
        validation.fail("DATASET_BINDING", "DATASET_SPLITS_INVALID", "dataset.splits must be a non-empty string array", "$.dataset.splits")
        return [], bindings
    requested_case_ids_raw = dataset.get("case_ids")
    requested_case_ids: set[str] | None = None
    if requested_case_ids_raw is not None:
        if (
            not isinstance(requested_case_ids_raw, list)
            or not requested_case_ids_raw
            or not all(isinstance(item, str) and item for item in requested_case_ids_raw)
            or len(set(requested_case_ids_raw)) != len(requested_case_ids_raw)
        ):
            validation.fail(
                "DATASET_BINDING",
                "DATASET_CASE_IDS_INVALID",
                "dataset.case_ids must be a non-empty unique string array when provided",
                "$.dataset.case_ids",
            )
            return [], bindings
        requested_case_ids = set(requested_case_ids_raw)

    entries_by_split = {
        entry.get("split"): entry
        for entry in manifest.get("files", [])
        if isinstance(entry, dict) and isinstance(entry.get("split"), str)
    }
    cases: list[dict[str, Any]] = []
    file_hashes: dict[str, str] = {}
    sealed_label_manifest_hashes: dict[str, str] = {}
    case_ids: list[str] = []
    seen_case_ids: set[str] = set()
    for split in selected_splits:
        entry = entries_by_split.get(split)
        if not isinstance(entry, dict):
            validation.fail(
                "DATASET_BINDING",
                "DATASET_SPLIT_MISSING",
                f"Selected split {split!r} is not present in the manifest",
                "$.dataset.splits",
            )
            continue
        input_name = entry.get("inputs")
        if not isinstance(input_name, str):
            validation.fail("DATASET_BINDING", "DATASET_INPUT_PATH_MISSING", f"Split {split!r} has no input path")
            continue
        input_path = _resolve_inside(
            manifest_path.parent,
            input_name,
            validation,
            "DATASET_BINDING",
            "DATASET_INPUT_PATH_OUTSIDE_ROOT",
        )
        if input_path is None:
            continue
        try:
            file_hashes[input_name] = _sha256_bytes(input_path.read_bytes())
        except OSError as exc:
            validation.fail("DATASET_BINDING", "DATASET_INPUT_READ_FAILED", f"Cannot read {input_path}: {exc}", str(input_path))
            continue
        rows = _read_jsonl(input_path, validation, "DATASET_BINDING", "DATASET_INPUT")
        declared_count = entry.get("case_count")
        if declared_count != len(rows):
            validation.fail(
                "DATASET_BINDING",
                "DATASET_CASE_COUNT_MISMATCH",
                f"Split {split!r} declares {declared_count!r} cases but contains {len(rows)}",
                str(input_path),
            )
        if split == "frozen_blind":
            if "labels" in entry:
                validation.fail(
                    "BLIND_ISOLATION",
                    "REPOSITORY_BLIND_LABEL_PAYLOAD_EXPOSED",
                    "frozen_blind must reference only a metadata seal, never a repository label payload",
                    "$.dataset.splits",
                )
            seal_name = entry.get("labels_seal")
            if not isinstance(seal_name, str) or not seal_name:
                validation.fail(
                    "BLIND_ISOLATION",
                    "BLIND_LABEL_SEAL_PATH_MISSING",
                    "frozen_blind requires a checked-in metadata-only label seal",
                    "$.dataset.splits",
                )
            else:
                seal_path = _resolve_inside(
                    manifest_path.parent,
                    seal_name,
                    validation,
                    "BLIND_ISOLATION",
                    "BLIND_LABEL_SEAL_PATH_OUTSIDE_DATASET",
                )
                if seal_path is not None:
                    try:
                        seal_bytes = seal_path.read_bytes()
                        seal = json.loads(seal_bytes)
                    except (OSError, json.JSONDecodeError) as exc:
                        validation.fail(
                            "BLIND_ISOLATION",
                            "BLIND_LABEL_SEAL_READ_FAILED",
                            f"Cannot read blind label seal: {exc}",
                            str(seal_path),
                        )
                    else:
                        seal_hash = _sha256_bytes(seal_bytes)
                        sealed_label_manifest_hashes[seal_name] = seal_hash
                        if entry.get("labels_seal_sha256") != seal_hash:
                            validation.fail(
                                "BLIND_ISOLATION",
                                "BLIND_LABEL_SEAL_SHA256_MISMATCH",
                                "Blind label seal hash does not match the dataset manifest",
                                str(seal_path),
                            )
                        case_ids_for_split = sorted(
                            str(row.get("case_id"))
                            for row in rows
                            if isinstance(row.get("case_id"), str) and row.get("case_id")
                        )
                        seal_contract_valid = bool(
                            isinstance(seal, dict)
                            and seal.get("schema_version") == "dual-entry-sealed-label-manifest-v1"
                            and seal.get("split") == "frozen_blind"
                            and seal.get("scoring_payload_present") is False
                            and seal.get("external_bundle_required") is True
                            and seal.get("case_count") == len(rows)
                            and seal.get("case_ids_sha256") == _sha256_json(case_ids_for_split)
                            and isinstance(seal.get("labels_canonical_sha256"), str)
                            and _SHA256_RE.fullmatch(seal["labels_canonical_sha256"])
                            and "labels" not in seal
                        )
                        if not seal_contract_valid:
                            validation.fail(
                                "BLIND_ISOLATION",
                                "BLIND_LABEL_SEAL_CONTRACT_INVALID",
                                "Blind label seal must be metadata-only and bind the selected case set",
                                str(seal_path),
                            )
        for row in rows:
            case_id = row.get("case_id")
            if not isinstance(case_id, str) or not case_id:
                validation.fail("DATASET_BINDING", "CASE_ID_MISSING", "Selected case has no case_id", str(input_path))
                continue
            if row.get("split") != split:
                validation.fail(
                    "DATASET_BINDING",
                    "CASE_SPLIT_MISMATCH",
                    f"Case {case_id} declares split {row.get('split')!r}, expected {split!r}",
                    str(input_path),
                )
            if requested_case_ids is not None and case_id not in requested_case_ids:
                continue
            if case_id in seen_case_ids:
                validation.fail("DATASET_BINDING", "CASE_ID_DUPLICATE", f"Duplicate case_id {case_id}", str(input_path))
            seen_case_ids.add(case_id)
            case_ids.append(case_id)
            cases.append(row)

    if requested_case_ids is not None:
        missing_case_ids = sorted(requested_case_ids - seen_case_ids)
        if missing_case_ids:
            validation.fail(
                "DATASET_BINDING",
                "DATASET_CASE_IDS_NOT_FOUND",
                f"Requested case_ids are not present in selected splits: {', '.join(missing_case_ids)}",
                "$.dataset.case_ids",
            )

    oracle_hashes: dict[str, str] = {}
    oracle_binding = dataset.get("graded_ranking_oracle")
    if oracle_binding is not None:
        if (
            not isinstance(oracle_binding, dict)
            or spec.get("lane") not in {"pr_offline", "nightly_snapshot"}
            or dataset.get("label_access") != "development_scorer"
            or oracle_binding.get("scope") != "development_only"
        ):
            validation.fail(
                "GRADED_RANKING_ORACLE_BINDING",
                "GRADED_RANKING_ORACLE_SCOPE_INVALID",
                "graded ranking oracle is allowed only for the development scorer lanes",
                "$.dataset.graded_ranking_oracle",
            )
        else:
            oracle_path = _resolve_inside(
                repo_root,
                str(oracle_binding.get("path") or ""),
                validation,
                "GRADED_RANKING_ORACLE_BINDING",
                "GRADED_RANKING_ORACLE_PATH_OUTSIDE_REPO",
            )
            source_snapshot_path = _resolve_inside(
                repo_root,
                str(oracle_binding.get("source_snapshot_path") or ""),
                validation,
                "GRADED_RANKING_ORACLE_BINDING",
                "GRADED_RANKING_ORACLE_SOURCE_PATH_OUTSIDE_REPO",
            )
            if oracle_path is not None and source_snapshot_path is not None:
                try:
                    oracle_sha256 = _sha256_bytes(oracle_path.read_bytes())
                    source_snapshot_sha256 = _sha256_bytes(source_snapshot_path.read_bytes())
                except OSError as exc:
                    validation.fail(
                        "GRADED_RANKING_ORACLE_BINDING",
                        "GRADED_RANKING_ORACLE_READ_FAILED",
                        f"Cannot read graded ranking oracle binding: {exc}",
                    )
                else:
                    if oracle_binding.get("sha256") != oracle_sha256:
                        validation.fail(
                            "GRADED_RANKING_ORACLE_BINDING",
                            "GRADED_RANKING_ORACLE_SHA256_MISMATCH",
                            "graded ranking oracle bytes changed after RunSpec binding",
                            str(oracle_path),
                        )
                    if oracle_binding.get("source_snapshot_sha256") != source_snapshot_sha256:
                        validation.fail(
                            "GRADED_RANKING_ORACLE_BINDING",
                            "GRADED_RANKING_ORACLE_SOURCE_SHA256_MISMATCH",
                            "graded ranking oracle source snapshot changed after RunSpec binding",
                            str(source_snapshot_path),
                        )
                    try:
                        from evals.frozen_suggestion_oracle import load_bound_oracle

                        artifact = load_bound_oracle(oracle_binding, repo_root)
                    except (OSError, ValueError, json.JSONDecodeError) as exc:
                        validation.fail(
                            "GRADED_RANKING_ORACLE_BINDING",
                            "GRADED_RANKING_ORACLE_VALIDATION_FAILED",
                            f"graded ranking oracle cannot be recomputed from its source: {exc}",
                            str(oracle_path),
                        )
                    else:
                        oracle_hashes = {
                            "artifact_file_sha256": oracle_sha256,
                            "artifact_content_sha256": str(artifact.get("content_sha256")),
                            "source_snapshot_sha256": source_snapshot_sha256,
                        }

    case_ids_hash = _sha256_json(sorted(case_ids))
    _bind_or_verify(dataset, "case_ids_sha256", case_ids_hash, validation, "$.dataset.case_ids_sha256")
    dataset_content_hash = _sha256_json(
        {
            "manifest_sha256": manifest_hash,
            "input_files": file_hashes,
            "sealed_label_manifests": sealed_label_manifest_hashes,
            "graded_ranking_oracle": oracle_hashes,
        }
    )
    bindings.update(
        {
            "manifest_sha256": manifest_hash,
            "case_ids_sha256": case_ids_hash,
            "dataset_content_sha256": dataset_content_hash,
            "selected_case_count": len(cases),
            "selected_input_file_sha256": file_hashes,
            "sealed_label_manifest_sha256": sealed_label_manifest_hashes,
            "graded_ranking_oracle": oracle_hashes,
        }
    )

    source_registry_raw = manifest.get("source_registry")
    if not isinstance(source_registry_raw, str) or not source_registry_raw:
        validation.fail("SOURCE_BINDING", "SOURCE_REGISTRY_PATH_MISSING", "Manifest must declare source_registry")
        return cases, bindings
    source_path = _resolve_inside(
        manifest_path.parent,
        source_registry_raw,
        validation,
        "SOURCE_BINDING",
        "SOURCE_REGISTRY_PATH_OUTSIDE_ROOT",
    )
    if source_path is None:
        return cases, bindings
    try:
        source_bytes = source_path.read_bytes()
    except OSError as exc:
        validation.fail("SOURCE_BINDING", "SOURCE_REGISTRY_READ_FAILED", f"Cannot read source registry: {exc}", str(source_path))
        return cases, bindings
    source_rows = _read_jsonl(source_path, validation, "SOURCE_BINDING", "SOURCE_REGISTRY")
    by_id: dict[str, dict[str, Any]] = {}
    for row in source_rows:
        source_id = row.get("source_document_id")
        if not isinstance(source_id, str) or not source_id:
            validation.fail("SOURCE_BINDING", "SOURCE_ID_MISSING", "Source registry row has no source_document_id", str(source_path))
            continue
        if source_id in by_id:
            validation.fail("SOURCE_BINDING", "SOURCE_ID_DUPLICATE", f"Duplicate source_document_id {source_id}", str(source_path))
        by_id[source_id] = row

    referenced_ids = sorted(
        {
            source_id
            for case in cases
            for source_id in case.get("source_document_refs", [])
            if isinstance(source_id, str)
        }
    )
    referenced_hashes: dict[str, dict[str, str | None]] = {}
    for source_id in referenced_ids:
        row = by_id.get(source_id)
        if row is None:
            validation.fail(
                "SOURCE_BINDING",
                "REFERENCED_SOURCE_MISSING",
                f"Case references unknown source document {source_id}",
                str(source_path),
            )
            continue
        raw_hash = row.get("raw_hash")
        extract_hash = row.get("extract_hash")
        if str(row.get("access_status", "")).upper() in {"UNAVAILABLE", "UNAVAILABLE_ON_CHECK"}:
            validation.fail(
                "SOURCE_BINDING",
                "REFERENCED_SOURCE_UNAVAILABLE",
                f"Referenced source {source_id} is unavailable and cannot contribute a scored derivation",
                str(source_path),
            )
        if not isinstance(raw_hash, str) or not _SHA256_RE.fullmatch(raw_hash):
            validation.fail(
                "SOURCE_BINDING",
                "REFERENCED_SOURCE_RAW_HASH_MISSING",
                f"Referenced source {source_id} has no concrete raw_hash",
                str(source_path),
            )
        if not isinstance(extract_hash, str) or not _SHA256_RE.fullmatch(extract_hash):
            validation.fail(
                "SOURCE_BINDING",
                "REFERENCED_SOURCE_EXTRACT_HASH_MISSING",
                f"Referenced source {source_id} has no concrete extract_hash",
                str(source_path),
            )
        for archive_kind, archive_key, expected_hash in (
            ("raw", "raw_archive_path", raw_hash),
            ("extract", "extract_archive_path", extract_hash),
        ):
            archive_value = row.get(archive_key)
            if not isinstance(archive_value, str) or not archive_value:
                validation.fail(
                    "SOURCE_BINDING",
                    f"REFERENCED_SOURCE_{archive_kind.upper()}_ARCHIVE_PATH_MISSING",
                    f"Referenced source {source_id} has no {archive_key}",
                    str(source_path),
                )
                continue
            archive_path = _resolve_inside(
                manifest_path.parent,
                archive_value,
                validation,
                "SOURCE_BINDING",
                f"REFERENCED_SOURCE_{archive_kind.upper()}_ARCHIVE_PATH_OUTSIDE_ROOT",
            )
            if archive_path is None:
                continue
            try:
                archive_bytes = archive_path.read_bytes()
            except OSError as exc:
                validation.fail(
                    "SOURCE_BINDING",
                    f"REFERENCED_SOURCE_{archive_kind.upper()}_ARCHIVE_READ_FAILED",
                    f"Cannot read {archive_kind} archive for {source_id}: {exc}",
                    str(archive_path),
                )
                continue
            if isinstance(expected_hash, str) and _SHA256_RE.fullmatch(expected_hash):
                actual_hash = _sha256_bytes(archive_bytes)
                if actual_hash != expected_hash:
                    validation.fail(
                        "SOURCE_BINDING",
                        f"REFERENCED_SOURCE_{archive_kind.upper()}_ARCHIVE_HASH_MISMATCH",
                        f"{archive_kind.capitalize()} archive hash mismatch for {source_id}",
                        str(archive_path),
                    )
        referenced_hashes[source_id] = {
            "raw_hash": raw_hash if isinstance(raw_hash, str) else None,
            "extract_hash": extract_hash if isinstance(extract_hash, str) else None,
        }

    bindings.update(
        {
            "source_registry_sha256": _sha256_bytes(source_bytes),
            "source_records_sha256": _sha256_json(source_rows),
            "referenced_source_hashes": referenced_hashes,
            "referenced_sources_sha256": _sha256_json(referenced_hashes),
        }
    )
    return cases, bindings


def preflight(
    spec_path: str | Path,
    *,
    repo_root: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> PreflightResult:
    path = Path(spec_path).resolve()
    root = Path(repo_root).resolve() if repo_root is not None else _discover_repo_root(path.parent)
    runtime_env = _effective_runtime_env(root, environ)
    validation = _Validation()
    try:
        source_bytes = path.read_bytes()
        loaded = json.loads(source_bytes)
    except (OSError, json.JSONDecodeError) as exc:
        validation.fail("RUN_SPEC_CONTRACT", "RUN_SPEC_READ_FAILED", f"Cannot load RunSpec: {exc}", str(path))
        return PreflightResult(path, root, None, None, {}, validation.checks(), tuple(validation.failures))

    source_hash = _sha256_bytes(source_bytes)
    if not _validate_top_level(loaded, validation):
        resolved = copy.deepcopy(loaded) if isinstance(loaded, dict) else None
        return PreflightResult(path, root, source_hash, resolved, {}, validation.checks(), tuple(validation.failures))
    spec = copy.deepcopy(loaded)
    _validate_required_placeholders(spec, validation)

    commit, dirty_hash = _git_bindings(root, validation)
    runtime_hash = _runtime_config_hash(spec, runtime_env)
    sut = spec.get("sut") if isinstance(spec.get("sut"), dict) else {}
    if isinstance(sut, dict):
        _bind_or_verify(sut, "commit_sha", commit, validation, "$.sut.commit_sha")
        _bind_or_verify(sut, "dirty_diff_sha256", dirty_hash, validation, "$.sut.dirty_diff_sha256")
        _bind_or_verify(sut, "runtime_config_sha256", runtime_hash, validation, "$.sut.runtime_config_sha256")
        if commit and dirty_hash:
            image_fingerprint = "local-worktree@sha256:" + _sha256_json(
                {"commit_sha": commit, "dirty_diff_sha256": dirty_hash, "runtime_config_sha256": runtime_hash}
            )
            _bind_or_verify(sut, "docker_image_digest", image_fingerprint, validation, "$.sut.docker_image_digest")

    cases, data_bindings = _dataset_and_sources(spec, root, validation)
    snapshot_bindings = _validate_frozen_snapshot_artifact(spec, root, validation)
    bindings: dict[str, Any] = {
        "sut_commit": commit,
        "dirty_diff_sha256": dirty_hash,
        "runtime_config_sha256": runtime_hash,
        **data_bindings,
        **snapshot_bindings,
    }
    execution = spec.get("execution") if isinstance(spec.get("execution"), dict) else {}
    if isinstance(execution, dict):
        cache_hash = _sha256_json(
            {
                "sut_commit": commit,
                "dirty_diff_sha256": dirty_hash,
                "runtime_config_sha256": runtime_hash,
                "dataset_content_sha256": data_bindings.get("dataset_content_sha256"),
                "source_registry_sha256": data_bindings.get("source_registry_sha256"),
                "referenced_sources_sha256": data_bindings.get("referenced_sources_sha256"),
                "provider": spec.get("provider"),
                "models": spec.get("models"),
            }
        )
        _bind_or_verify(execution, "cache_namespace_sha256", cache_hash, validation, "$.execution.cache_namespace_sha256")
        bindings["cache_namespace_sha256"] = cache_hash
        execution["bindings"] = copy.deepcopy(bindings)

    _validate_models(spec, validation)
    _validate_blind_isolation(spec, cases, validation)
    _validate_sql_seed(spec, cases, validation)
    _validate_provider_mode(spec, cases, runtime_env, validation)
    _validate_no_auto_placeholders_remain(spec, validation)
    bindings["resolved_run_spec_sha256"] = _sha256_json(spec)
    return PreflightResult(
        spec_path=path,
        repo_root=root,
        source_spec_sha256=source_hash,
        resolved_spec=spec,
        bindings=bindings,
        checks=validation.checks(),
        errors=tuple(validation.failures),
    )


def _atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _canonical_bytes(value) + b"\n"
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _new_run_dir(runs_root: Path, lane: str) -> tuple[str, Path]:
    runs_root.mkdir(parents=True, exist_ok=True)
    for _attempt in range(20):
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        run_id = f"{timestamp}-{lane}-{secrets.token_hex(4)}"
        run_dir = runs_root / run_id
        try:
            run_dir.mkdir()
        except FileExistsError:
            continue
        return run_id, run_dir
    raise RuntimeError("Unable to allocate a unique continuous-eval run directory")


def run_foundation(
    spec_path: str | Path,
    *,
    runs_root: str | Path | None = None,
    repo_root: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> RunResult:
    started_at = datetime.now(timezone.utc).isoformat()
    result = preflight(spec_path, repo_root=repo_root, environ=environ)
    lane = "invalid"
    if isinstance(result.resolved_spec, dict) and isinstance(result.resolved_spec.get("lane"), str):
        lane = result.resolved_spec["lane"]
    output_root = Path(runs_root).resolve() if runs_root is not None else result.repo_root / "backend" / "evidence" / "runs"
    run_id, run_dir = _new_run_dir(output_root, lane)

    artifact_spec: dict[str, Any]
    if result.resolved_spec is not None:
        artifact_spec = copy.deepcopy(result.resolved_spec)
    else:
        artifact_spec = {
            "schema_version": "invalid-run-spec-v1",
            "source_path": str(result.spec_path),
            "source_spec_sha256": result.source_spec_sha256,
        }
    artifact_spec["run_id"] = run_id
    artifact_spec["started_at"] = started_at
    _atomic_write_json(run_dir / "run_spec.json", artifact_spec)
    artifact_spec_hash = _sha256_bytes((run_dir / "run_spec.json").read_bytes())

    if result.valid:
        reason = "PRODUCT_ADAPTER_AND_STAGES_NOT_IMPLEMENTED"
        execution_errors = [
            {
                "code": "PRODUCT_CHAIN_UNAVAILABLE",
                "message": "Preflight passed, but no registered HTTP product adapter and stage executor exist in this foundation.",
            }
        ]
    else:
        reason = "PREFLIGHT_FAILED"
        execution_errors = list(result.errors)
    completed_at = datetime.now(timezone.utc).isoformat()
    gate: dict[str, Any] = {
        "schema_version": "continuous-gate-v1",
        "run_id": run_id,
        "lane": lane,
        "status": "INVALID",
        "decision": "REJECT",
        "phase": "PREFLIGHT" if not result.valid else "EXECUTION_NOT_AVAILABLE",
        "started_at": started_at,
        "completed_at": completed_at,
        "run_spec_artifact_sha256": artifact_spec_hash,
        "bindings": result.bindings,
        "gates": [
            *result.checks,
            {
                "id": "PRODUCT_EXECUTION",
                "status": "NOT_RUN" if not result.valid else "FAIL",
                "reason": reason,
            },
        ],
        "failed_cases": [],
        "baseline_run_id": (
            result.resolved_spec.get("comparison", {}).get("baseline_run_id")
            if isinstance(result.resolved_spec, dict) and isinstance(result.resolved_spec.get("comparison"), dict)
            else None
        ),
        "confidence_interval": None,
        "execution": {
            "attempted": False,
            "product_http_calls": 0,
            "adapter": None,
            "stages": [],
            "reason": reason,
        },
        "errors": execution_errors,
    }
    _atomic_write_json(run_dir / "gate.json", gate)
    return RunResult(run_id=run_id, run_dir=run_dir, gate=gate)
