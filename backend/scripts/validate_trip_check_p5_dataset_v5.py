"""Build or validate the label-free P5 v5 dataset manifest."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from evals.trip_check_v1.p5.data_contract import canonical_bytes, file_sha256  # noqa: E402
from evals.trip_check_v1.p5.data_contract_v5 import (  # noqa: E402
    BLIND_INPUT_PATH_V5,
    BLIND_MATERIALIZATIONS_PATH_V5,
    BLIND_SEAL_PATH_V5,
    MANIFEST_PATH_V5,
    P5DataContractErrorV5,
    build_pending_manifest_v5,
    validate_manifest_v5,
)
from evals.trip_check_v1.p5.dataset_contracts_v5 import P5BlindSealV5  # noqa: E402


REPO_ROOT = BACKEND_ROOT.parent
ACTIVE_CONTRACT_PATH = BACKEND_ROOT / "evals" / "trip_check_v1" / "p5" / "active_contract.json"
_BLIND_FORBIDDEN_KEYS = {"oracle", "label", "labels", "expected", "answer"}


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_suffix(path.suffix + ".pending")
    pending.write_bytes(canonical_bytes(value) + b"\n")
    pending.replace(path)


def _load_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise P5DataContractErrorV5(f"invalid JSON object: {path.name}")
    return value


def _scan_forbidden(value: object) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower() in _BLIND_FORBIDDEN_KEYS:
                found.add(str(key).lower())
            found.update(_scan_forbidden(item))
    elif isinstance(value, list):
        for item in value:
            found.update(_scan_forbidden(item))
    return found


def validate(*, formal: bool) -> dict:
    errors: list[str] = []
    try:
        manifest = validate_manifest_v5(
            REPO_ROOT, manifest_path=MANIFEST_PATH_V5, require_sealed=formal
        )
        for path in (BLIND_INPUT_PATH_V5, BLIND_MATERIALIZATIONS_PATH_V5):
            for line in path.read_text(encoding="utf-8").splitlines():
                if _scan_forbidden(json.loads(line)):
                    raise P5DataContractErrorV5("blind label-like key found in repository payload")
        if formal:
            seal = P5BlindSealV5.model_validate(_load_json(BLIND_SEAL_PATH_V5))
            active = _load_json(ACTIVE_CONTRACT_PATH)
            commitment = manifest["sealing_commitment"]
            if (
                active.get("active_contract") != "trip-check-p5-v5"
                or active.get("formal_evidence_status") != "READY"
                or active.get("dataset_manifest_hash") != manifest["manifest_hash"]
                or active.get("blind_seal_v5_sha256") != file_sha256(BLIND_SEAL_PATH_V5)
                or commitment.get("blind_seal_file_sha256")
                != file_sha256(BLIND_SEAL_PATH_V5)
                or commitment.get("candidate_freeze_commit")
                != seal.candidate_freeze_commit
            ):
                raise P5DataContractErrorV5("sealed manifest/active contract binding mismatch")
    except (OSError, UnicodeError, json.JSONDecodeError, P5DataContractErrorV5, ValueError) as exc:
        manifest = {}
        errors.append(type(exc).__name__)
    return {
        "schema_version": "trip-check-p5-dataset-validation-v5",
        "status": "PASS" if not errors else "REJECT",
        "formal": formal,
        "dataset_id": "trip-check-p5-360-v5",
        "manifest_hash": manifest.get("manifest_hash"),
        "counts": manifest.get("counts"),
        "v4_payload_byte_identity": not errors,
        "blind_label_like_key_count": 0 if not errors else None,
        "errors": errors,
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--write-pending", action="store_true")
    result.add_argument("--formal", action="store_true")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.write_pending:
        if args.formal:
            raise SystemExit("--write-pending and --formal are mutually exclusive")
        _write_json(MANIFEST_PATH_V5, build_pending_manifest_v5(REPO_ROOT))
    result = validate(formal=args.formal)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
