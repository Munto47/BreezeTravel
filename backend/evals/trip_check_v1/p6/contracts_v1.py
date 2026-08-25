"""Fail-closed P6 CandidateRunSpec, public evidence, and release contracts."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import subprocess
import urllib.error
import urllib.request
import unicodedata
from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime
from pathlib import Path, PureWindowsPath
from typing import Any, Literal
from urllib.parse import urlparse

from jsonschema import Draft202012Validator, FormatChecker


P6_ROOT = Path(__file__).resolve().parent
SCHEMA_PATHS = {
    "candidate_run_spec": P6_ROOT / "candidate_run_spec_v1.schema.json",
    "candidate_evidence": P6_ROOT / "candidate_evidence_v1.schema.json",
    "release_manifest": P6_ROOT / "release_manifest_v1.schema.json",
    "candidate_gate_readback": P6_ROOT / "candidate_gate_readback_v1.schema.json",
    "gate_receipt": P6_ROOT / "gate_receipt_v1.schema.json",
    "public_receipt": P6_ROOT / "public_receipt_v1.schema.json",
    "candidate_gate_receipt": P6_ROOT / "candidate_gate_receipt_v1.schema.json",
    "final_disclosure_readback": P6_ROOT / "final_disclosure_readback_v1.schema.json",
    "real_ocr_dataset_manifest": P6_ROOT / "real_ocr_dataset_manifest_v1.schema.json",
}
P5_GATE_MANIFEST_HASH = "9a3338a565522577f4514f628b225ad165e87085a992185bd2650b197011187a"
GATE_KEYS = tuple(f"g{index}" for index in range(7))
RELEASE_GATE_KEYS = tuple(f"g{index}" for index in range(6))
GATE_EVIDENCE_LEVELS = {
    "g0": "repository_contract",
    "g1": "real_authorized_ocr",
    "g2": "postgresql_integration",
    "g3": "controlled_snapshot",
    "g4": "live_provider",
    "g5": "browser_local",
}
PASS = "PASS"
P6_UPSTREAM_REF = "origin/codex/trip-check-p6-candidate-evidence"
P6_EVIDENCE_ROOT_PARENT = PureWindowsPath(
    "D:/munto/code/claudeProject/agentTravel-p6-artifacts/p6-candidate"
)


class P6ContractError(RuntimeError):
    """Stable fail-closed P6 contract error."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


def _normalize(value: Any) -> Any:
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n"))
    if isinstance(value, Mapping):
        return {str(key): _normalize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize(item) for item in value]
    return value


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        _normalize(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def file_sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise P6ContractError("P6_ARTIFACT_UNREADABLE") from exc


def _load_json(path: Path, reason: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise P6ContractError(reason) from exc
    if not isinstance(value, dict):
        raise P6ContractError(reason)
    return value


def _schema(kind: str) -> dict[str, Any]:
    path = SCHEMA_PATHS.get(kind)
    if path is None:
        raise P6ContractError("P6_SCHEMA_KIND_INVALID")
    return _load_json(path, "P6_SCHEMA_UNREADABLE")


def validate_schemas() -> dict[str, str]:
    hashes: dict[str, str] = {}
    for kind, path in SCHEMA_PATHS.items():
        schema = _schema(kind)
        try:
            Draft202012Validator.check_schema(schema)
        except Exception as exc:  # jsonschema exposes several schema-error subclasses
            raise P6ContractError("P6_SCHEMA_INVALID") from exc
        hashes[kind] = file_sha256(path)
    return hashes


def _validate_schema(value: Mapping[str, Any], kind: str, reason: str) -> None:
    errors = sorted(
        Draft202012Validator(_schema(kind), format_checker=FormatChecker()).iter_errors(value),
        key=lambda item: tuple(str(part) for part in item.path),
    )
    if errors:
        raise P6ContractError(reason)


def _validate_self_hash(value: Mapping[str, Any], field: str, reason: str) -> None:
    expected = digest({key: item for key, item in value.items() if key != field})
    if value.get(field) != expected:
        raise P6ContractError(reason)


def _validate_repo_binding(value: Mapping[str, Any], reason: str) -> None:
    subject = value.get("subject_commit")
    upstream = value.get("upstream_commit")
    upstream_ref = value.get("upstream_ref")
    if (
        not isinstance(subject, str)
        or re.fullmatch(r"[0-9a-f]{40}", subject) is None
        or upstream != subject
        or upstream_ref != P6_UPSTREAM_REF
        or value.get("dirty_tree") is not False
    ):
        raise P6ContractError(reason)


def _validate_https(value: object, reason: str) -> None:
    if not isinstance(value, str):
        raise P6ContractError(reason)
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise P6ContractError(reason)
    hostname = parsed.hostname
    if hostname is None or hostname.lower() == "localhost" or hostname.lower().endswith(".localhost"):
        raise P6ContractError(reason)
    lowered_host = hostname.lower().rstrip(".")
    blocked_suffixes = (".local", ".internal", ".lan", ".home", ".test", ".invalid", ".example")
    if "." not in lowered_host or lowered_host.endswith(blocked_suffixes):
        raise P6ContractError(reason)
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        if not address.is_global:
            raise P6ContractError(reason)


def _validate_gate_keys(gates: object, reason: str) -> Mapping[str, Any]:
    if not isinstance(gates, Mapping) or tuple(sorted(gates)) != GATE_KEYS:
        raise P6ContractError(reason)
    return gates


def _validate_release_gate_keys(gates: object, reason: str) -> Mapping[str, Any]:
    if not isinstance(gates, Mapping) or tuple(sorted(gates)) != RELEASE_GATE_KEYS:
        raise P6ContractError(reason)
    return gates


def _validate_actual_repo_state(
    claimed: Mapping[str, Any],
    actual: Mapping[str, Any],
    reason: str,
) -> None:
    required = ("subject_commit", "upstream_ref", "upstream_commit", "dirty_tree")
    if set(actual) != set(required) or any(actual[key] != claimed[key] for key in required):
        raise P6ContractError(reason)


def _path_is_absolute_on_any_platform(value: str) -> bool:
    return Path(value).is_absolute() or PureWindowsPath(value).is_absolute()


def _path_has_parent_escape(value: str) -> bool:
    return ".." in Path(value).parts or ".." in PureWindowsPath(value).parts


def validate_candidate_run_spec(value: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(value)
    _validate_schema(payload, "candidate_run_spec", "P6_CANDIDATE_RUN_SPEC_SCHEMA_INVALID")
    _validate_self_hash(payload, "run_spec_hash", "P6_CANDIDATE_RUN_SPEC_HASH_MISMATCH")
    _validate_repo_binding(payload, "P6_CANDIDATE_RUN_SPEC_REPO_BINDING_INVALID")
    if payload["p5_gate_manifest_hash"] != P5_GATE_MANIFEST_HASH:
        raise P6ContractError("P6_P5_GATE_BINDING_INVALID")
    matrix = payload["provider_live_matrix"]
    if sum(matrix[key] for key in ("amap_route_calls", "qweather_forecast_calls", "qweather_alert_calls")) != 18:
        raise P6ContractError("P6_LIVE_MATRIX_INVALID")
    _validate_https(payload["public_candidate"]["base_url"], "P6_PUBLIC_BASE_URL_INVALID")
    evidence_root = payload["evidence_root"]
    if not _path_is_absolute_on_any_platform(evidence_root) or _path_has_parent_escape(evidence_root):
        raise P6ContractError("P6_EVIDENCE_ROOT_INVALID")
    normalized = PureWindowsPath(evidence_root)
    if normalized.parent != P6_EVIDENCE_ROOT_PARENT or normalized.name != payload["subject_commit"]:
        raise P6ContractError("P6_EVIDENCE_ROOT_SUBJECT_BINDING_INVALID")
    return payload


def validate_real_ocr_dataset_manifest(value: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(value)
    _validate_schema(
        payload,
        "real_ocr_dataset_manifest",
        "P6_REAL_OCR_DATASET_SCHEMA_INVALID",
    )
    _validate_self_hash(
        payload,
        "manifest_hash",
        "P6_REAL_OCR_DATASET_HASH_MISMATCH",
    )
    items = payload["items"]
    city_counts = {city: 0 for city in ("北京", "上海", "杭州")}
    unique_fields = (
        "item_id",
        "source_image_sha256",
        "source_group_hash",
        "perceptual_hash",
    )
    for field in unique_fields:
        values = [item[field] for item in items]
        if len(values) != len(set(values)):
            raise P6ContractError("P6_REAL_OCR_DATASET_DUPLICATE")
    fingerprints = [int(item["perceptual_hash"], 16) for item in items]
    for index, fingerprint in enumerate(fingerprints):
        if any((fingerprint ^ other).bit_count() <= 4 for other in fingerprints[index + 1 :]):
            raise P6ContractError("P6_REAL_OCR_DATASET_NEAR_DUPLICATE")
    for item in items:
        city_counts[item["city"]] += 1
        expected_authorization = {
            "RIGHTSHOLDER_OWNED": "RIGHTSHOLDER_ATTESTATION",
            "OPEN_LICENSE": "OPEN_LICENSE",
            "PUBLIC_DOMAIN": "PUBLIC_DOMAIN",
            "EXPLICIT_PERMISSION": "WRITTEN_PERMISSION",
        }[item["provenance_class"]]
        if item["authorization_basis"] != expected_authorization:
            raise P6ContractError("P6_REAL_OCR_AUTHORIZATION_BINDING_INVALID")
        if item["annotation_version"] != payload["annotation_version"]:
            raise P6ContractError("P6_REAL_OCR_ANNOTATION_BINDING_INVALID")
        if item["ocr_config_sha256"] != payload["ocr_config_sha256"]:
            raise P6ContractError("P6_REAL_OCR_CONFIG_BINDING_INVALID")
    if city_counts != {"北京": 20, "上海": 20, "杭州": 20}:
        raise P6ContractError("P6_REAL_OCR_CITY_DISTRIBUTION_INVALID")
    return payload


def validate_real_ocr_dataset_binding(
    value: Mapping[str, Any],
    candidate_run_spec: Mapping[str, Any],
    manifest_file_sha256: str,
) -> dict[str, Any]:
    payload = validate_real_ocr_dataset_manifest(value)
    spec = validate_candidate_run_spec(candidate_run_spec)
    if (
        re.fullmatch(r"[0-9a-f]{64}", manifest_file_sha256) is None
        or payload["subject_commit"] != spec["subject_commit"]
        or manifest_file_sha256 != spec["bindings"]["ocr_dataset_manifest_sha256"]
    ):
        raise P6ContractError("P6_REAL_OCR_RUN_SPEC_BINDING_INVALID")
    return payload


def read_actual_repo_state(repo_root: Path) -> dict[str, Any]:
    def git(*args: str) -> str:
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=repo_root,
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            raise P6ContractError("P6_GIT_READBACK_FAILED") from exc
        return result.stdout.strip()

    upstream_short = git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")
    local_upstream_commit = git("rev-parse", "@{upstream}")
    remote_line = git(
        "ls-remote",
        "--heads",
        "origin",
        "refs/heads/codex/trip-check-p6-candidate-evidence",
    )
    remote_parts = remote_line.split()
    if len(remote_parts) != 2 or re.fullmatch(r"[0-9a-f]{40}", remote_parts[0]) is None:
        raise P6ContractError("P6_GIT_REMOTE_READBACK_FAILED")
    if remote_parts[0] != local_upstream_commit:
        raise P6ContractError("P6_GIT_REMOTE_CHECKPOINT_MISMATCH")
    return {
        "subject_commit": git("rev-parse", "HEAD"),
        "upstream_ref": upstream_short,
        "upstream_commit": remote_parts[0],
        "dirty_tree": bool(git("status", "--porcelain=v1", "--untracked-files=all")),
    }


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201
        raise urllib.error.HTTPError(req.full_url, code, "redirect rejected", headers, fp)


def read_public_candidate(base_url: str) -> dict[str, Any]:
    opener = urllib.request.build_opener(_RejectRedirects())

    def fetch(route: str) -> tuple[int, bytes]:
        request = urllib.request.Request(
            f"{base_url}{route}",
            method="GET",
            headers={"Accept": "application/json", "User-Agent": "BreezeTravel-P6-Gate/1"},
        )
        try:
            with opener.open(request, timeout=15) as response:
                return response.status, response.read()
        except (OSError, urllib.error.URLError, urllib.error.HTTPError) as exc:
            raise P6ContractError("P6_PUBLIC_READBACK_FAILED") from exc

    health_status, health_body = fetch("/health")
    evidence_status, evidence_body = fetch("/api/evidence/latest")
    try:
        evidence_payload = json.loads(evidence_body.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise P6ContractError("P6_PUBLIC_READBACK_INVALID_JSON") from exc
    if not isinstance(evidence_payload, dict):
        raise P6ContractError("P6_PUBLIC_READBACK_INVALID_JSON")
    return {
        "health_http_status": health_status,
        "evidence_http_status": evidence_status,
        "health_response_body_sha256": hashlib.sha256(health_body).hexdigest(),
        "evidence_response_body_sha256": hashlib.sha256(evidence_body).hexdigest(),
        "candidate_evidence": evidence_payload,
    }


def validate_candidate_evidence(value: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(value)
    _validate_schema(payload, "candidate_evidence", "P6_CANDIDATE_EVIDENCE_SCHEMA_INVALID")
    gates = _validate_gate_keys(payload["gates"], "P6_CANDIDATE_EVIDENCE_GATES_INVALID")
    _validate_https(payload["public_e2e"]["url"], "P6_PUBLIC_E2E_URL_INVALID")
    if payload["candidate_gate_status"] == PASS:
        levels = payload["evidence_levels"]
        if (
            any(status != PASS for status in gates.values())
            or any(status != PASS for status in levels.values())
            or payload["public_e2e"]["status"] != PASS
            or payload["public_e2e"]["health_status"] != PASS
        ):
            raise P6ContractError("P6_CANDIDATE_EVIDENCE_PREMATURE_PASS")
        if not isinstance(payload["candidate_gate_receipt_hash"], str):
            raise P6ContractError("P6_CANDIDATE_GATE_RECEIPT_MISSING")
    elif payload["candidate_gate_receipt_hash"] is not None:
        raise P6ContractError("P6_CANDIDATE_GATE_RECEIPT_PREMATURE")
    if "HUMAN_EVIDENCE_NOT_RUN" not in payload["known_gaps"]:
        raise P6ContractError("P6_HUMAN_EVIDENCE_GAP_MISSING")
    return payload


def validate_release_manifest(value: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(value)
    _validate_schema(payload, "release_manifest", "P6_RELEASE_MANIFEST_SCHEMA_INVALID")
    _validate_self_hash(payload, "manifest_hash", "P6_RELEASE_MANIFEST_HASH_MISMATCH")
    _validate_repo_binding(payload, "P6_RELEASE_MANIFEST_REPO_BINDING_INVALID")
    if payload["p5_gate_manifest_hash"] != P5_GATE_MANIFEST_HASH:
        raise P6ContractError("P6_P5_GATE_BINDING_INVALID")
    gates = _validate_release_gate_keys(payload["gates"], "P6_RELEASE_MANIFEST_GATES_INVALID")
    _validate_https(payload["public_e2e"]["url"], "P6_PUBLIC_E2E_URL_INVALID")
    logical_names: set[str] = set()
    artifact_paths: set[str] = set()
    artifacts_by_name: dict[str, Mapping[str, Any]] = {}
    for artifact in payload["artifacts"]:
        path = artifact["path"]
        if _path_is_absolute_on_any_platform(path) or _path_has_parent_escape(path):
            raise P6ContractError("P6_RELEASE_ARTIFACT_PATH_INVALID")
        if artifact["logical_name"] in logical_names or path in artifact_paths:
            raise P6ContractError("P6_RELEASE_ARTIFACT_DUPLICATE")
        logical_names.add(artifact["logical_name"])
        artifact_paths.add(path)
        artifacts_by_name[artifact["logical_name"]] = artifact
    required_receipts = {
        *(f"{gate}_receipt" for gate in RELEASE_GATE_KEYS),
        "public_health_receipt",
        "public_e2e_receipt",
    }
    if not required_receipts.issubset(logical_names):
        raise P6ContractError("P6_RELEASE_GATE_RECEIPT_MISSING")
    for gate, receipt in gates.items():
        artifact = artifacts_by_name[f"{gate}_receipt"]
        if (
            receipt["receipt_sha256"] != artifact["sha256"]
            or receipt["evidence_level"] != GATE_EVIDENCE_LEVELS[gate]
            or artifact["evidence_level"] != GATE_EVIDENCE_LEVELS[gate]
        ):
            raise P6ContractError("P6_RELEASE_GATE_RECEIPT_UNBOUND")
    health_artifact = artifacts_by_name["public_health_receipt"]
    e2e_artifact = artifacts_by_name["public_e2e_receipt"]
    if (
        payload["public_e2e"]["health_receipt_sha256"] != health_artifact["sha256"]
        or payload["public_e2e"]["e2e_receipt_sha256"] != e2e_artifact["sha256"]
        or health_artifact["evidence_level"] != "public_e2e"
        or e2e_artifact["evidence_level"] != "public_e2e"
    ):
        raise P6ContractError("P6_PUBLIC_E2E_RECEIPT_UNBOUND")
    if "HUMAN_EVIDENCE_NOT_RUN" not in payload["known_gaps"]:
        raise P6ContractError("P6_HUMAN_EVIDENCE_GAP_MISSING")
    return payload


def _validate_gate_receipt_payload(
    receipt: Mapping[str, Any],
    gate: str,
    spec: Mapping[str, Any],
) -> None:
    _validate_schema(receipt, "gate_receipt", "P6_GATE_RECEIPT_SCHEMA_INVALID")
    _validate_self_hash(receipt, "receipt_hash", "P6_GATE_RECEIPT_HASH_MISMATCH")
    if (
        receipt["gate"] != gate
        or receipt["subject_commit"] != spec["subject_commit"]
        or receipt["run_spec_hash"] != spec["run_spec_hash"]
        or receipt["evidence_level"] != GATE_EVIDENCE_LEVELS[gate]
        or receipt["checks_passed"] != receipt["checks_total"]
    ):
        raise P6ContractError("P6_GATE_RECEIPT_BINDING_INVALID")
    metrics = receipt["metrics"]
    gate_requirements = {
        "g0": {"authority_conflict_count": 0},
        "g1": {
            "authorized_source_count": 60,
            "beijing_count": 20,
            "shanghai_count": 20,
            "hangzhou_count": 20,
            "privacy_leak_count": 0,
            "cleanup_failure_count": 0,
        },
        "g2": {
            "migration_failure_count": 0,
            "transaction_failure_count": 0,
            "restart_readback_failure_count": 0,
            "concurrency_failure_count": 0,
        },
        "g3": {"network_call_count": 0, "replay_mismatch_count": 0},
        "g4": {
            "network_call_count": 18,
            "provider_receipt_count": 18,
            "amap_route_call_count": 12,
            "qweather_forecast_call_count": 3,
            "qweather_alert_call_count": 3,
            "fixture_fallback_count": 0,
            "provider_failure_count": 0,
        },
        "g5": {
            "local_browser_failure_count": 0,
            "public_e2e_failure_count": 0,
            "performance_threshold_failure_count": 0,
            "privacy_failure_count": 0,
        },
    }
    if any(metrics.get(key) != expected for key, expected in gate_requirements[gate].items()):
        raise P6ContractError("P6_GATE_RECEIPT_METRICS_INVALID")
    if gate == "g1" and (
        metrics.get("key_field_micro_f1", 0) < 0.95
        or metrics.get("must_confirm_recall") != 1
        or metrics.get("work_copy_cleanup_count") != 60
    ):
        raise P6ContractError("P6_GATE_RECEIPT_METRICS_INVALID")


def _validate_public_receipt_payload(
    receipt: Mapping[str, Any],
    kind: str,
    spec: Mapping[str, Any],
) -> None:
    _validate_schema(receipt, "public_receipt", "P6_PUBLIC_RECEIPT_SCHEMA_INVALID")
    _validate_self_hash(receipt, "receipt_hash", "P6_PUBLIC_RECEIPT_HASH_MISMATCH")
    expected_target = "/health" if kind == "health" else "trip_check_full_chain"
    if (
        receipt["kind"] != kind
        or receipt["subject_commit"] != spec["subject_commit"]
        or receipt["run_spec_hash"] != spec["run_spec_hash"]
        or receipt["base_url"] != spec["public_candidate"]["base_url"]
        or receipt["target"] != expected_target
    ):
        raise P6ContractError("P6_PUBLIC_RECEIPT_BINDING_INVALID")


def validate_release_artifact_files(
    payload: Mapping[str, Any],
    artifact_root: Path,
    spec: Mapping[str, Any],
) -> None:
    try:
        resolved_root = artifact_root.resolve(strict=True)
    except OSError as exc:
        raise P6ContractError("P6_RELEASE_ARTIFACT_ROOT_UNREADABLE") from exc
    for artifact in payload["artifacts"]:
        try:
            artifact_path = (resolved_root / artifact["path"]).resolve(strict=True)
            artifact_path.relative_to(resolved_root)
            size_bytes = artifact_path.stat().st_size
        except (OSError, ValueError) as exc:
            raise P6ContractError("P6_RELEASE_ARTIFACT_UNREADABLE") from exc
        if size_bytes != artifact["size_bytes"] or file_sha256(artifact_path) != artifact["sha256"]:
            raise P6ContractError("P6_RELEASE_ARTIFACT_READBACK_MISMATCH")
        receipt = _load_json(artifact_path, "P6_RELEASE_RECEIPT_INVALID")
        logical_name = artifact["logical_name"]
        if logical_name in {f"{gate}_receipt" for gate in RELEASE_GATE_KEYS}:
            _validate_gate_receipt_payload(receipt, logical_name[:2], spec)
        elif logical_name == "public_health_receipt":
            _validate_public_receipt_payload(receipt, "health", spec)
        elif logical_name == "public_e2e_receipt":
            _validate_public_receipt_payload(receipt, "e2e", spec)


def validate_candidate_gate_readback(value: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(value)
    _validate_schema(payload, "candidate_gate_readback", "P6_CANDIDATE_GATE_READBACK_SCHEMA_INVALID")
    _validate_self_hash(payload, "receipt_hash", "P6_CANDIDATE_GATE_READBACK_HASH_MISMATCH")
    _validate_https(payload["url"], "P6_PUBLIC_E2E_URL_INVALID")
    return payload


def validate_candidate_gate_receipt(value: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(value)
    _validate_schema(payload, "candidate_gate_receipt", "P6_CANDIDATE_GATE_RECEIPT_SCHEMA_INVALID")
    _validate_self_hash(payload, "receipt_hash", "P6_CANDIDATE_GATE_RECEIPT_HASH_MISMATCH")
    return payload


def validate_final_disclosure_readback(value: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(value)
    _validate_schema(payload, "final_disclosure_readback", "P6_FINAL_DISCLOSURE_SCHEMA_INVALID")
    _validate_self_hash(payload, "receipt_hash", "P6_FINAL_DISCLOSURE_HASH_MISMATCH")
    _validate_https(payload["url"], "P6_PUBLIC_E2E_URL_INVALID")
    return payload


def validate_candidate_gate_decision(
    candidate_gate_receipt: Mapping[str, Any],
    pre_gate_evidence: Mapping[str, Any],
    release_manifest: Mapping[str, Any],
    g6_readback_receipt: Mapping[str, Any],
) -> dict[str, Any]:
    receipt = validate_candidate_gate_receipt(candidate_gate_receipt)
    public = validate_candidate_evidence(pre_gate_evidence)
    release = validate_release_manifest(release_manifest)
    readback = validate_candidate_gate_readback(g6_readback_receipt)
    decided_at = datetime.fromisoformat(receipt["decided_at"].replace("Z", "+00:00"))
    observed_at = datetime.fromisoformat(readback["observed_at"].replace("Z", "+00:00"))
    if (
        public["candidate_gate_status"] != "NOT_RUN"
        or public["gates"]["g6"] != "NOT_RUN"
        or any(public["gates"][gate] != PASS for gate in RELEASE_GATE_KEYS)
        or any(status != PASS for status in public["evidence_levels"].values())
        or public["public_e2e"]["status"] != PASS
        or public["public_e2e"]["health_status"] != PASS
        or any(item["status"] != PASS for item in release["gates"].values())
        or not (
            receipt["subject_commit"] == public["subject_commit"] == release["subject_commit"]
        )
        or receipt["upstream_ref"] != release["upstream_ref"]
        or receipt["upstream_commit"] != release["upstream_commit"]
        or receipt["dirty_tree"] is not False
        or receipt["run_spec_hash"] != release["candidate_run_spec_hash"]
        or not (
            receipt["manifest_hash"] == public["manifest_hash"] == release["manifest_hash"]
        )
        or readback["subject_commit"] != public["subject_commit"]
        or readback["manifest_hash"] != release["manifest_hash"]
        or readback["candidate_evidence_sha256"] != digest(public)
        or receipt["pre_gate_evidence_sha256"] != digest(public)
        or receipt["pre_gate_evidence_response_body_sha256"]
        != readback["evidence_response_body_sha256"]
        or receipt["pre_gate_health_response_body_sha256"]
        != readback["health_response_body_sha256"]
        or receipt["g6_readback_receipt_hash"] != readback["receipt_hash"]
        or public["known_gaps"] != release["known_gaps"]
        or public["scope"] != release["scope"]
        or public["public_e2e"]["url"] != release["public_e2e"]["url"]
        or decided_at <= observed_at
    ):
        raise P6ContractError("P6_CANDIDATE_GATE_DECISION_BINDING_INVALID")
    return receipt


def validate_final_candidate_evidence(
    final_evidence: Mapping[str, Any],
    pre_gate_evidence: Mapping[str, Any],
    release_manifest: Mapping[str, Any],
    candidate_gate_receipt: Mapping[str, Any],
    g6_readback_receipt: Mapping[str, Any],
) -> dict[str, Any]:
    public = validate_candidate_evidence(final_evidence)
    pre_gate = validate_candidate_evidence(pre_gate_evidence)
    receipt = validate_candidate_gate_decision(
        candidate_gate_receipt,
        pre_gate,
        release_manifest,
        g6_readback_receipt,
    )
    expected = deepcopy(pre_gate)
    expected["gates"]["g6"] = PASS
    expected["candidate_gate_status"] = PASS
    expected["candidate_gate_receipt_hash"] = receipt["receipt_hash"]
    if public != expected:
        raise P6ContractError("P6_FINAL_CANDIDATE_EVIDENCE_BINDING_INVALID")
    return public


def candidate_final_disclosure_valid(
    final_evidence: Mapping[str, Any],
    pre_gate_evidence: Mapping[str, Any],
    release_manifest: Mapping[str, Any],
    candidate_run_spec: Mapping[str, Any],
    candidate_gate_receipt: Mapping[str, Any],
    g6_readback_receipt: Mapping[str, Any],
    final_disclosure_readback: Mapping[str, Any],
    repo_root: Path,
) -> bool:
    spec = validate_candidate_run_spec(candidate_run_spec)
    release = validate_release_manifest(release_manifest)
    final = validate_final_candidate_evidence(
        final_evidence,
        pre_gate_evidence,
        release,
        candidate_gate_receipt,
        g6_readback_receipt,
    )
    gate_receipt = validate_candidate_gate_receipt(candidate_gate_receipt)
    disclosure = validate_final_disclosure_readback(final_disclosure_readback)
    validate_release_artifact_files(release, Path(spec["evidence_root"]), spec)
    actual_repo_state = read_actual_repo_state(repo_root)
    _validate_actual_repo_state(spec, actual_repo_state, "P6_ACTUAL_REPO_BINDING_INVALID")
    _validate_actual_repo_state(release, actual_repo_state, "P6_ACTUAL_REPO_BINDING_INVALID")
    actual_public = read_public_candidate(spec["public_candidate"]["base_url"])
    decided_at = datetime.fromisoformat(gate_receipt["decided_at"].replace("Z", "+00:00"))
    observed_at = datetime.fromisoformat(disclosure["observed_at"].replace("Z", "+00:00"))
    return bool(
        release["candidate_run_spec_hash"] == spec["run_spec_hash"]
        and release["p5_gate_manifest_hash"] == spec["p5_gate_manifest_hash"]
        and final["subject_commit"] == release["subject_commit"] == spec["subject_commit"]
        and final["scope"] == release["scope"] == spec["scope"]
        and final["known_gaps"] == release["known_gaps"]
        and final["public_e2e"]["url"]
        == release["public_e2e"]["url"]
        == spec["public_candidate"]["base_url"]
        and actual_public["candidate_evidence"] == final
        and disclosure["subject_commit"] == spec["subject_commit"]
        and disclosure["manifest_hash"] == release["manifest_hash"]
        and disclosure["candidate_gate_receipt_hash"] == gate_receipt["receipt_hash"]
        and disclosure["final_evidence_sha256"] == digest(final)
        and disclosure["url"] == spec["public_candidate"]["base_url"]
        and disclosure["health_http_status"] == actual_public["health_http_status"]
        and disclosure["evidence_http_status"] == actual_public["evidence_http_status"]
        and disclosure["health_response_body_sha256"]
        == actual_public["health_response_body_sha256"]
        and disclosure["evidence_response_body_sha256"]
        == actual_public["evidence_response_body_sha256"]
        and observed_at > decided_at
    )


def candidate_gate_eligible(
    evidence: Mapping[str, Any],
    manifest: Mapping[str, Any],
    run_spec: Mapping[str, Any],
    readback_receipt: Mapping[str, Any],
    repo_root: Path,
) -> bool:
    public = validate_candidate_evidence(evidence)
    release = validate_release_manifest(manifest)
    spec = validate_candidate_run_spec(run_spec)
    readback = validate_candidate_gate_readback(readback_receipt)
    validate_release_artifact_files(release, Path(spec["evidence_root"]), spec)
    actual_repo_state = read_actual_repo_state(repo_root)
    actual_public = read_public_candidate(spec["public_candidate"]["base_url"])
    _validate_actual_repo_state(spec, actual_repo_state, "P6_ACTUAL_REPO_BINDING_INVALID")
    _validate_actual_repo_state(release, actual_repo_state, "P6_ACTUAL_REPO_BINDING_INVALID")
    released_at = datetime.fromisoformat(release["released_at"].replace("Z", "+00:00"))
    observed_at = datetime.fromisoformat(readback["observed_at"].replace("Z", "+00:00"))
    gates = _validate_gate_keys(public["gates"], "P6_CANDIDATE_EVIDENCE_GATES_INVALID")
    release_gates = _validate_release_gate_keys(release["gates"], "P6_RELEASE_MANIFEST_GATES_INVALID")
    return bool(
        public["subject_commit"] == release["subject_commit"] == spec["subject_commit"]
        and public["manifest_hash"] == release["manifest_hash"]
        and release["candidate_run_spec_hash"] == spec["run_spec_hash"]
        and release["p5_gate_manifest_hash"] == spec["p5_gate_manifest_hash"]
        and public["scope"] == release["scope"] == spec["scope"]
        and readback["subject_commit"] == spec["subject_commit"]
        and readback["manifest_hash"] == release["manifest_hash"]
        and readback["candidate_evidence_sha256"] == digest(public)
        and actual_public["candidate_evidence"] == public
        and readback["health_http_status"] == actual_public["health_http_status"]
        and readback["evidence_http_status"] == actual_public["evidence_http_status"]
        and readback["health_response_body_sha256"] == actual_public["health_response_body_sha256"]
        and readback["evidence_response_body_sha256"] == actual_public["evidence_response_body_sha256"]
        and observed_at > released_at
        and public["candidate_gate_status"] == "NOT_RUN"
        and public["candidate_gate_receipt_hash"] is None
        and public["public_e2e"]["status"] == PASS
        and release["public_e2e"]["status"] == PASS
        and all(gates[gate] == PASS for gate in RELEASE_GATE_KEYS)
        and gates["g6"] == "NOT_RUN"
        and all(status == PASS for status in public["evidence_levels"].values())
        and all(item["status"] == PASS for item in release_gates.values())
        and public["public_e2e"]["health_status"] == PASS
        and public["public_e2e"]["url"]
        == release["public_e2e"]["url"]
        == spec["public_candidate"]["base_url"]
        == readback["url"]
        and public["known_gaps"] == release["known_gaps"]
        and public["human_evidence"] is False
        and release["human_evidence"] is False
        and release["read_only_mount"] is True
    )


def load_and_validate(
    path: Path,
    kind: Literal[
        "candidate_run_spec",
        "candidate_evidence",
        "release_manifest",
        "candidate_gate_readback",
        "candidate_gate_receipt",
        "final_disclosure_readback",
        "real_ocr_dataset_manifest",
    ],
) -> dict[str, Any]:
    payload = _load_json(path, "P6_CONTRACT_ARTIFACT_INVALID")
    validators = {
        "candidate_run_spec": validate_candidate_run_spec,
        "candidate_evidence": validate_candidate_evidence,
        "release_manifest": validate_release_manifest,
        "candidate_gate_readback": validate_candidate_gate_readback,
        "candidate_gate_receipt": validate_candidate_gate_receipt,
        "final_disclosure_readback": validate_final_disclosure_readback,
        "real_ocr_dataset_manifest": validate_real_ocr_dataset_manifest,
    }
    return validators[kind](payload)
