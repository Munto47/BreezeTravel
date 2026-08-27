from __future__ import annotations

import hashlib
import json
from pathlib import Path

from evals.trip_nlu_v2.validator import _current_code_bindings


BACKEND_ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = BACKEND_ROOT / "eval_data" / "trip_nlu_v2"
OUTPUT = (
    BACKEND_ROOT
    / "eval_data"
    / "trip_nlu_v2_remediation"
    / "candidate_manifest.json"
)


def main() -> None:
    historical_path = DATA_ROOT / "manifest.json"
    historical = json.loads(historical_path.read_text(encoding="utf-8"))
    candidate = {
        **historical,
        "schema_version": "trip-nlu-v2-candidate-manifest-v1",
        "candidate_binding_only": True,
        "base_dataset_manifest_sha256": hashlib.sha256(
            historical_path.read_bytes()
        ).hexdigest(),
        "code_bindings": _current_code_bindings(BACKEND_ROOT),
    }
    OUTPUT.write_text(
        json.dumps(candidate, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


if __name__ == "__main__":
    main()
