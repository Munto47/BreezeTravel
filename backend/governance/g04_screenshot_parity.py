from __future__ import annotations

import hashlib
import json
import math
import re
import subprocess
from pathlib import Path
from typing import Any


FORMAL_RECEIPT_PATH = "backend/governance/g04_screenshot_parity_receipt.json"
FORMAL_EVIDENCE_KEY = "g04_formal_parity_receipt"
FORMAL_SCHEMA_VERSION = "g04-screenshot-parity-receipt-v1"
FORMAL_EVIDENCE_LEVEL = "REAL_PADDLE_LICENSED_SCREENSHOT_PARITY"
FORMAL_EXECUTION_MODE = "REAL_PADDLE_LOCAL"
G04_GOAL_ID = "TC-VNEXT-G04-SCREENSHOT"

_SHA256 = re.compile(r"[0-9a-f]{64}")
_COMMIT = re.compile(r"[0-9a-f]{40}")
_PRODUCT_ROOTS = (
    "backend/app/",
    "frontend/src/",
    "miniapp/src/",
    "packages/trip-check-client/src/",
)
_PRODUCT_CONFIG_PATHS = {
    ".env.example",
    "backend/requirements-base.txt",
    "backend/requirements.txt",
    "docker-compose.yml",
    "frontend/next.config.js",
    "frontend/package-lock.json",
    "frontend/package.json",
    "miniapp/package-lock.json",
    "miniapp/package.json",
    "packages/trip-check-client/package-lock.json",
    "packages/trip-check-client/package.json",
}


class G04ParityReceiptError(ValueError):
    pass


def _require_keys(value: Any, expected: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise G04ParityReceiptError(f"{label} fields are invalid")
    return value


def _require_hash(value: Any, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise G04ParityReceiptError(f"{label} is not a SHA-256")
    return value


def _require_positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise G04ParityReceiptError(f"{label} must be a positive integer")
    return value


def _require_unit_interval(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise G04ParityReceiptError(f"{label} must be numeric")
    number = float(value)
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        raise G04ParityReceiptError(f"{label} must be in [0, 1]")
    return number


def _git(root: Path, *args: str, binary: bool = False) -> str | bytes:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=False,
        capture_output=True,
        text=not binary,
        encoding=None if binary else "utf-8",
        errors=None if binary else "replace",
    )
    if result.returncode != 0:
        stderr = (
            result.stderr.decode("utf-8", errors="replace")
            if binary
            else result.stderr
        )
        raise G04ParityReceiptError(stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout


def _product_fingerprint_at_commit(root: Path, commit: str) -> str:
    names = str(_git(root, "ls-tree", "-r", "--name-only", commit)).splitlines()
    paths = sorted(
        path
        for path in names
        if path in _PRODUCT_CONFIG_PATHS
        or any(path.startswith(prefix) for prefix in _PRODUCT_ROOTS)
    )
    digest = hashlib.sha256()
    for relative in paths:
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        blob_id = str(_git(root, "rev-parse", f"{commit}:{relative}")).strip()
        digest.update(blob_id.encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def _sha256_at_commit(root: Path, commit: str, relative: str) -> str:
    value = _git(root, "show", f"{commit}:{relative}", binary=True)
    return hashlib.sha256(bytes(value)).hexdigest()


def canonical_receipt_hash(receipt: dict[str, Any]) -> str:
    payload = {key: value for key, value in receipt.items() if key != "receipt_hash"}
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_formal_receipt(receipt: dict[str, Any]) -> None:
    _require_keys(
        receipt,
        {
            "schema_version",
            "goal_id",
            "evidence_level",
            "execution_mode",
            "sanitized",
            "candidate",
            "evaluator",
            "source_policy",
            "paddle",
            "hardware",
            "performance",
            "metric_counts",
            "metrics",
            "decision",
            "receipt_hash",
        },
        "formal receipt",
    )
    if receipt["schema_version"] != FORMAL_SCHEMA_VERSION:
        raise G04ParityReceiptError("formal receipt schema is invalid")
    if receipt["goal_id"] != G04_GOAL_ID:
        raise G04ParityReceiptError("formal receipt binds the wrong Goal")
    if receipt["evidence_level"] != FORMAL_EVIDENCE_LEVEL:
        raise G04ParityReceiptError("fixture or informal evidence cannot prove parity")
    if receipt["execution_mode"] != FORMAL_EXECUTION_MODE:
        raise G04ParityReceiptError("formal parity was not run with real Paddle")
    if receipt["sanitized"] is not True:
        raise G04ParityReceiptError("formal receipt is not declared sanitized")

    candidate = _require_keys(
        receipt["candidate"],
        {"commit", "tree", "product_fingerprint"},
        "candidate",
    )
    if not isinstance(candidate["commit"], str) or _COMMIT.fullmatch(candidate["commit"]) is None:
        raise G04ParityReceiptError("candidate commit is invalid")
    if not isinstance(candidate["tree"], str) or _COMMIT.fullmatch(candidate["tree"]) is None:
        raise G04ParityReceiptError("candidate tree is invalid")
    _require_hash(candidate["product_fingerprint"], "candidate product fingerprint")

    evaluator = _require_keys(
        receipt["evaluator"],
        {
            "baseline_manifest_sha256",
            "expected_transcript_sha256",
            "runspec_sha256",
            "runner_sha256",
            "scorer_sha256",
            "text_only_day_dataset_sha256",
            "oracle_adjudication_sha256",
            "metric_inputs_sha256",
            "scored_outputs_sha256",
        },
        "evaluator",
    )
    for key, value in evaluator.items():
        _require_hash(value, f"evaluator.{key}")

    source = _require_keys(
        receipt["source_policy"],
        {
            "originals_in_git",
            "originals_storage",
            "license_manifest_sha256",
            "cleanup_receipts_sha256",
            "terminal_cleanup_receipts_complete",
            "parity_metric_scope",
            "review_label",
            "human_review",
            "candidate_outputs_used_for_oracle",
        },
        "source policy",
    )
    if source["originals_in_git"] is not False:
        raise G04ParityReceiptError("source originals must not enter Git")
    if source["originals_storage"] != "OUTSIDE_GIT_EPHEMERAL":
        raise G04ParityReceiptError("source originals storage policy is invalid")
    if source["terminal_cleanup_receipts_complete"] is not True:
        raise G04ParityReceiptError("source original cleanup is incomplete")
    if source["parity_metric_scope"] != "LICENSED_REAL_ONLY":
        raise G04ParityReceiptError("formal parity metrics must use licensed real cases only")
    if source["review_label"] != "MULTI_AGENT_SIMULATED_REVIEW":
        raise G04ParityReceiptError("oracle review evidence is mislabeled")
    if source["human_review"] is not False:
        raise G04ParityReceiptError("G04 may not claim human evidence")
    if source["candidate_outputs_used_for_oracle"] is not False:
        raise G04ParityReceiptError("candidate output cannot be its own oracle")
    _require_hash(source["license_manifest_sha256"], "license manifest")
    _require_hash(source["cleanup_receipts_sha256"], "cleanup receipts")

    paddle = _require_keys(
        receipt["paddle"],
        {
            "paddleocr_version",
            "paddlepaddle_version",
            "model_sha256",
            "config_sha256",
        },
        "Paddle binding",
    )
    if paddle["paddleocr_version"] != "3.7.0" or paddle["paddlepaddle_version"] != "3.3.1":
        raise G04ParityReceiptError("Paddle version binding is stale")
    _require_hash(paddle["model_sha256"], "Paddle model binding")
    _require_hash(paddle["config_sha256"], "Paddle config binding")

    hardware = _require_keys(
        receipt["hardware"],
        {"device_class", "hardware_sha256", "driver_sha256"},
        "hardware binding",
    )
    if hardware["device_class"] not in {"LOCAL_CPU", "LOCAL_GPU"}:
        raise G04ParityReceiptError("hardware device class is invalid")
    _require_hash(hardware["hardware_sha256"], "hardware binding")
    _require_hash(hardware["driver_sha256"], "driver binding")

    performance = _require_keys(
        receipt["performance"],
        {
            "image_count",
            "width_px",
            "height_px",
            "concurrency",
            "warmup_runs",
            "measured_runs",
            "measurement_ms",
            "p95_ms",
        },
        "performance binding",
    )
    expected_ints = {
        "image_count": 3,
        "width_px": 1080,
        "height_px": 1920,
        "concurrency": 1,
        "warmup_runs": 2,
        "measured_runs": 20,
    }
    if any(performance[key] != expected for key, expected in expected_ints.items()):
        raise G04ParityReceiptError("performance RunSpec is not the frozen 3x1080x1920 2+20 contract")
    samples = performance["measurement_ms"]
    if (
        not isinstance(samples, list)
        or len(samples) != 20
        or any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or float(value) <= 0
            for value in samples
        )
    ):
        raise G04ParityReceiptError("performance samples are missing or invalid")
    calculated_p95 = sorted(float(value) for value in samples)[18]
    reported_p95 = performance["p95_ms"]
    if (
        isinstance(reported_p95, bool)
        or not isinstance(reported_p95, (int, float))
        or not math.isclose(float(reported_p95), calculated_p95, abs_tol=0.001)
        or calculated_p95 > 12_000
    ):
        raise G04ParityReceiptError("performance P95 is invalid or exceeds 12 seconds")

    counts = _require_keys(
        receipt["metric_counts"],
        {
            "case_count",
            "licensed_real_case_count",
            "synthetic_case_count",
            "text_only_day_case_count",
            "critical_field_count",
            "low_confidence_critical_field_count",
            "reading_adjacency_count",
            "location_baseline_count",
            "cleanup_terminal_count",
            "cleanup_receipt_count",
        },
        "metric counts",
    )
    for key, value in counts.items():
        _require_positive_int(value, f"metric_counts.{key}")
    if counts["case_count"] != counts["licensed_real_case_count"] + counts["synthetic_case_count"]:
        raise G04ParityReceiptError("case counts are inconsistent")
    if counts["cleanup_terminal_count"] != counts["cleanup_receipt_count"]:
        raise G04ParityReceiptError("cleanup receipt denominator is incomplete")

    metrics = _require_keys(
        receipt["metrics"],
        {
            "critical_field_f1",
            "low_confidence_confirmation_recall",
            "reading_order_adjacency_f1",
            "location_precision_drop_pp",
            "location_recall_drop_pp",
            "wrong_city_count",
            "wrong_category_count",
            "sentence_as_place_count",
            "internal_leak_count",
            "cleanup_receipt_coverage",
        },
        "metrics",
    )
    if _require_unit_interval(metrics["critical_field_f1"], "critical field F1") < 0.95:
        raise G04ParityReceiptError("critical field F1 is below 95%")
    if _require_unit_interval(
        metrics["low_confidence_confirmation_recall"],
        "low-confidence confirmation recall",
    ) != 1.0:
        raise G04ParityReceiptError("low-confidence confirmation recall is below 100%")
    if _require_unit_interval(
        metrics["reading_order_adjacency_f1"],
        "reading-order adjacency F1",
    ) < 0.97:
        raise G04ParityReceiptError("reading-order adjacency F1 is below 97%")
    for key in ("location_precision_drop_pp", "location_recall_drop_pp"):
        value = metrics[key]
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or not 0.0 <= float(value) <= 1.0
        ):
            raise G04ParityReceiptError(f"{key} exceeds one percentage point")
    for key in (
        "wrong_city_count",
        "wrong_category_count",
        "sentence_as_place_count",
        "internal_leak_count",
    ):
        if metrics[key] != 0:
            raise G04ParityReceiptError(f"{key} must be zero")
    if _require_unit_interval(metrics["cleanup_receipt_coverage"], "cleanup receipt coverage") != 1.0:
        raise G04ParityReceiptError("cleanup receipt coverage is below 100%")

    decision = _require_keys(receipt["decision"], {"status", "failures"}, "decision")
    if decision["status"] != "PASS" or decision["failures"] != []:
        raise G04ParityReceiptError("formal parity is not PASS")
    if receipt["receipt_hash"] != canonical_receipt_hash(receipt):
        raise G04ParityReceiptError("formal receipt hash is invalid")


def validate_g04_delivery_evidence(
    repository_root: Path,
    delivery_receipt: dict[str, Any],
    *,
    expected_product_fingerprint: str,
    current_product_fingerprint: str | None,
) -> dict[str, Any]:
    root = repository_root.resolve()
    evidence_ref = delivery_receipt.get(FORMAL_EVIDENCE_KEY)
    evidence = _require_keys(
        evidence_ref,
        {"path", "sha256", "candidate_commit", "product_fingerprint"},
        "G04 delivery evidence reference",
    )
    if evidence["path"] != FORMAL_RECEIPT_PATH:
        raise G04ParityReceiptError("G04 formal receipt path is invalid")
    _require_hash(evidence["sha256"], "G04 formal receipt file hash")
    if evidence["product_fingerprint"] != expected_product_fingerprint:
        raise G04ParityReceiptError("delivery and formal evidence fingerprints differ")

    receipt_path = root / FORMAL_RECEIPT_PATH
    try:
        receipt_bytes = receipt_path.read_bytes()
        formal = json.loads(receipt_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise G04ParityReceiptError("G04 formal parity receipt is missing or invalid") from exc
    if not isinstance(formal, dict):
        raise G04ParityReceiptError("G04 formal parity receipt root is invalid")
    if hashlib.sha256(receipt_bytes).hexdigest() != evidence["sha256"]:
        raise G04ParityReceiptError("G04 formal parity receipt file hash is stale")
    tracked_path = str(
        _git(root, "ls-files", "--error-unmatch", FORMAL_RECEIPT_PATH)
    ).strip()
    if tracked_path != FORMAL_RECEIPT_PATH:
        raise G04ParityReceiptError("G04 formal parity receipt is not tracked")
    _validate_formal_receipt(formal)

    candidate = formal["candidate"]
    if evidence["candidate_commit"] != candidate["commit"]:
        raise G04ParityReceiptError("delivery receipt binds a different candidate commit")
    if evidence["product_fingerprint"] != candidate["product_fingerprint"]:
        raise G04ParityReceiptError("formal receipt product fingerprint is inconsistent")
    resolved_commit = str(_git(root, "rev-parse", f"{candidate['commit']}^{{commit}}")).strip()
    if resolved_commit != candidate["commit"]:
        raise G04ParityReceiptError("candidate commit is unavailable")
    resolved_tree = str(_git(root, "rev-parse", f"{candidate['commit']}^{{tree}}")).strip()
    if resolved_tree != candidate["tree"]:
        raise G04ParityReceiptError("candidate tree binding is stale")
    ancestor = subprocess.run(
        ["git", "-C", str(root), "merge-base", "--is-ancestor", candidate["commit"], "HEAD"],
        check=False,
        capture_output=True,
    )
    if ancestor.returncode != 0:
        raise G04ParityReceiptError("candidate commit is not an ancestor of HEAD")
    if _product_fingerprint_at_commit(root, candidate["commit"]) != candidate["product_fingerprint"]:
        raise G04ParityReceiptError("candidate commit does not match its product fingerprint")
    evaluator = formal["evaluator"]
    for field, relative in {
        "baseline_manifest_sha256": (
            "backend/eval_data/g04_screenshot/licensed_baseline_v1.json"
        ),
        "runner_sha256": "backend/scripts/run_g04_paddle_gate.py",
        "scorer_sha256": "backend/evals/g04_screenshot/scorer.py",
    }.items():
        if _sha256_at_commit(root, candidate["commit"], relative) != evaluator[field]:
            raise G04ParityReceiptError(f"{field} does not match the candidate commit")
    if (
        current_product_fingerprint is not None
        and candidate["product_fingerprint"] != current_product_fingerprint
    ):
        raise G04ParityReceiptError("formal parity evidence is stale for current product bytes")
    return formal
