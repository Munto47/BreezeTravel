from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest

from evals.trip_check_v1.p5.contracts_v2 import P5CaseV2, P5OracleV2
from evals.trip_check_v1.p5.data_contract import digest, file_sha256, load_jsonl
import evals.trip_check_v1.p5.data_contract_v2 as data_v2
from evals.trip_check_v1.p5.data_contract_v2 import (
    BLIND_INPUT_PATH_V2,
    BLIND_MATERIALIZATIONS_PATH_V2,
    MANIFEST_PATH_V2,
    NONBLIND_MATERIALIZATIONS_PATH_V2,
    NONBLIND_PATH_V2,
    case_set_hash,
    materialization_input_projection,
    materialization_set_hash,
)
from scripts.validate_trip_check_p5_dataset_v2 import validate


def test_checked_in_v2_development_dataset_has_complete_contract_and_distribution() -> None:
    result = validate(formal=False)

    assert result["status"] == "PASS", result["errors"]
    assert result["counts"] == {
        "total": 360,
        "by_split": {"dev": 180, "frozen_blind": 90, "pilot": 18, "regression": 72},
        "by_city": {"上海": 120, "北京": 120, "杭州": 120},
        "screenshots_by_split": {"pilot": 0, "dev": 90, "regression": 36, "frozen_blind": 45},
    }
    assert result["blind"]["unknown_required"] == 18
    assert result["blind"]["concurrency"] == 20
    assert result["legacy_overlap_debt"]["regression_fixture_hashes_overlapping_dev"] == 72
    assert result["legacy_overlap_debt"]["regression_oracle_hashes_overlapping_dev"] == 72


def test_formal_validator_rejects_development_ocr_receipts() -> None:
    result = validate(formal=True)

    assert result["status"] == "FAIL"
    assert "formal validation rejects development OCR artifacts" in result["errors"]
    assert sum("requires actual paddleocr 3.7.0" in item for item in result["errors"]) == 171


def test_manifest_exposes_strict_file_and_lane_set_hashes() -> None:
    manifest = json.loads(MANIFEST_PATH_V2.read_text(encoding="utf-8"))
    nonblind_cases = load_jsonl(NONBLIND_PATH_V2)
    blind_cases = load_jsonl(BLIND_INPUT_PATH_V2)
    nonblind_materializations = load_jsonl(NONBLIND_MATERIALIZATIONS_PATH_V2)
    blind_materializations = load_jsonl(BLIND_MATERIALIZATIONS_PATH_V2)

    for key, path, rows in (
        ("nonblind_cases", NONBLIND_PATH_V2, nonblind_cases),
        ("blind_cases", BLIND_INPUT_PATH_V2, blind_cases),
        ("nonblind_materializations", NONBLIND_MATERIALIZATIONS_PATH_V2, nonblind_materializations),
        ("blind_materializations", BLIND_MATERIALIZATIONS_PATH_V2, blind_materializations),
    ):
        assert manifest["files"][key]["file_sha256"] == file_sha256(path)
        assert manifest["files"][key]["content_sha256"] == digest(rows)
    assert manifest["lanes"]["nonblind"]["case_set_hash"] == case_set_hash(nonblind_cases)
    assert manifest["lanes"]["frozen_blind"]["case_set_hash"] == case_set_hash(blind_cases)
    assert manifest["lanes"]["nonblind"]["materialization_set_hash"] == materialization_set_hash(
        nonblind_materializations
    )
    assert manifest["lanes"]["frozen_blind"]["materialization_set_hash"] == materialization_set_hash(
        blind_materializations
    )
    assert manifest["manifest_hash"] == digest(
        {key: value for key, value in manifest.items() if key != "manifest_hash"}
    )


def test_nonblind_oracles_are_frozen_v2_and_blind_cases_have_no_oracle() -> None:
    for row in load_jsonl(NONBLIND_PATH_V2):
        case = P5CaseV2.model_validate(row)
        assert isinstance(case.oracle, P5OracleV2)
        assert row["oracle_sha256"] == digest(row["oracle"])
    for row in load_jsonl(BLIND_INPUT_PATH_V2):
        case = P5CaseV2.model_validate(row)
        assert case.oracle is None
        assert "oracle" not in row
        assert "oracle_sha256" not in row


def test_materialization_projection_cannot_iterate_or_read_label_fields() -> None:
    payload = {
        "case_id": "p5.dev.bj.001",
        "city": "北京",
        "trip_days": 2,
        "group_size": 2,
        "input_kind": "TEXT",
        "product_input": {"source_type": "MANUAL_TEXT", "raw_text": "北京2日行程"},
        "normalized_input_sha256": "1" * 64,
        "runner_control": {"seed": 1},
        "oracle": {"secret": True},
        "expected": {"secret": True},
    }

    class LabelGuard(Mapping[str, object]):
        def __getitem__(self, key: str) -> object:
            if key in {"oracle", "expected"}:
                raise AssertionError(f"label field was accessed: {key}")
            return payload[key]

        def __iter__(self) -> Iterator[str]:
            raise AssertionError("materialization projection must not iterate case payload")

        def __len__(self) -> int:
            return len(payload)

    projected = materialization_input_projection(LabelGuard())

    assert set(projected) == {
        "case_id",
        "city",
        "trip_days",
        "group_size",
        "input_kind",
        "product_input",
        "normalized_input_sha256",
        "runner_control",
    }
    assert "oracle" not in str(projected).casefold()
    assert "expected" not in str(projected).casefold()


def test_resumable_checkpoint_is_hash_bound_and_skips_completed_materialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    draft = next(item for item in data_v2.build_nonblind_drafts() if item["input_kind"] == "SYNTHETIC_SCREENSHOT")
    case = next(item for item in load_jsonl(NONBLIND_PATH_V2) if item["case_id"] == draft["case_id"])
    materialization = next(
        item for item in load_jsonl(NONBLIND_MATERIALIZATIONS_PATH_V2) if item["case_id"] == draft["case_id"]
    )
    checkpoint_root = tmp_path / "checkpoints"
    data_v2._write_checkpoint_pair(
        draft=draft,
        case=case,
        materialization=materialization,
        ocr_mode="development",
        checkpoint_root=checkpoint_root,
    )

    cached = data_v2._load_checkpoint_pair(
        draft=draft,
        ocr_mode="development",
        checkpoint_root=checkpoint_root,
    )
    assert cached == (case, materialization)

    monkeypatch.setattr(data_v2, "build_nonblind_drafts", lambda: [draft])
    monkeypatch.setattr(data_v2, "build_blind_drafts", lambda: [])

    async def must_not_materialize(*_args: object, **_kwargs: object) -> tuple[dict, dict]:
        raise AssertionError("completed checkpoint was materialized again")

    monkeypatch.setattr(data_v2, "_materialize_one", must_not_materialize)
    nonblind_cases, blind_cases, nonblind_materializations, blind_materializations = asyncio.run(
        data_v2.build_dataset_v2(
            ocr_mode="development",
            work_root=tmp_path / "work",
            checkpoint_root=checkpoint_root,
        )
    )
    assert nonblind_cases == [case]
    assert blind_cases == []
    assert nonblind_materializations == [materialization]
    assert blind_materializations == []


def test_resumable_checkpoint_rejects_rehashed_case_binding_tamper(tmp_path: Path) -> None:
    draft = next(item for item in data_v2.build_nonblind_drafts() if item["input_kind"] == "TEXT")
    case = next(item for item in load_jsonl(NONBLIND_PATH_V2) if item["case_id"] == draft["case_id"])
    materialization = next(
        item for item in load_jsonl(NONBLIND_MATERIALIZATIONS_PATH_V2) if item["case_id"] == draft["case_id"]
    )
    checkpoint_root = tmp_path / "checkpoints"
    data_v2._write_checkpoint_pair(
        draft=draft,
        case=case,
        materialization=materialization,
        ocr_mode="development",
        checkpoint_root=checkpoint_root,
    )
    checkpoint_path = data_v2._checkpoint_path(checkpoint_root, draft["case_id"])
    payload = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    payload["case"]["city"] = "上海"
    payload["checkpoint_sha256"] = digest({key: value for key, value in payload.items() if key != "checkpoint_sha256"})
    checkpoint_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )

    with pytest.raises(ValueError, match="checkpoint case binding mismatch"):
        data_v2._load_checkpoint_pair(
            draft=draft,
            ocr_mode="development",
            checkpoint_root=checkpoint_root,
        )
