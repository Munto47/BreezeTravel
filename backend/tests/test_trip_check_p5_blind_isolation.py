import copy
import json
from pathlib import Path

from evals.trip_check_v1.p5.data_contract import BLIND_INPUT_PATH, digest, load_jsonl
from scripts.validate_trip_check_p5_dataset import _BLIND_FORBIDDEN_KEYS, _walk_keys


def test_blind_inputs_never_contain_oracle_or_expected_payloads():
    rows = load_jsonl(BLIND_INPUT_PATH)

    assert len(rows) == 90
    assert all(not (_walk_keys(row) & _BLIND_FORBIDDEN_KEYS) for row in rows)
    assert all("oracle_sha256" not in row for row in rows)


def test_blind_case_hash_detects_input_tampering_without_reading_labels():
    row = load_jsonl(BLIND_INPUT_PATH)[0]
    changed = copy.deepcopy(row)
    changed["product_input"]["raw_text"] = "tampered"
    body = {key: value for key, value in changed.items() if key != "case_hash"}

    assert changed["case_hash"] != digest(body)


def test_blind_inputs_are_jsonl_objects_without_external_bundle_path():
    rows = [json.loads(line) for line in BLIND_INPUT_PATH.read_text(encoding="utf-8").splitlines()]

    assert all(isinstance(row, dict) for row in rows)
    assert all("bundle_path" not in _walk_keys(row) for row in rows)


def test_product_runner_source_cannot_import_isolated_blind_scorer():
    backend_root = Path(__file__).resolve().parents[1]
    product_runner_sources = (
        backend_root / "evals" / "trip_check_v1" / "p5" / "adapters.py",
        backend_root / "evals" / "trip_check_v1" / "p5" / "runner.py",
        backend_root / "scripts" / "run_trip_check_p5_eval.py",
    )

    for path in product_runner_sources:
        assert "final_blind_scorer" not in path.read_text(encoding="utf-8")
