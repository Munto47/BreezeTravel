from __future__ import annotations

from collections import Counter
from collections.abc import Iterator, Mapping
from copy import deepcopy
from functools import lru_cache

import pytest

from evals.trip_check_v1.p5.adapters_v3 import validate_materialization_v3
from evals.trip_check_v1.p5.contracts_v3 import P5CaseV3
from evals.trip_check_v1.p5.data_contract import digest, file_sha256, load_jsonl
from evals.trip_check_v1.p5.data_contract_v2 import (
    BLIND_INPUT_PATH_V2,
    BLIND_MATERIALIZATIONS_PATH_V2,
    BLIND_SEAL_PATH_V2,
    MANIFEST_PATH_V2,
    NONBLIND_MATERIALIZATIONS_PATH_V2,
    NONBLIND_PATH_V2,
)
from evals.trip_check_v1.p5.data_contract_v3 import (
    build_dataset_v3,
    build_manifest_v3,
    evidence_projection_v3,
    materialization_input_projection_v3,
    validate_v2_source_anchor,
)


@lru_cache(maxsize=1)
def _build() -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    return build_dataset_v3()


def test_v3_rebinds_all_360_cases_without_mutating_v2_bytes() -> None:
    v2_paths = (
        NONBLIND_PATH_V2,
        BLIND_INPUT_PATH_V2,
        NONBLIND_MATERIALIZATIONS_PATH_V2,
        BLIND_MATERIALIZATIONS_PATH_V2,
        MANIFEST_PATH_V2,
        BLIND_SEAL_PATH_V2,
    )
    before = {path: file_sha256(path) for path in v2_paths}

    nonblind, blind, nonblind_materializations, blind_materializations = _build()

    assert [len(rows) for rows in (nonblind, blind, nonblind_materializations, blind_materializations)] == [
        270,
        90,
        270,
        90,
    ]
    assert Counter(row["split"] for row in [*nonblind, *blind]) == Counter(
        {"pilot": 18, "dev": 180, "regression": 72, "frozen_blind": 90}
    )
    assert Counter(row["city"] for row in [*nonblind, *blind]) == Counter(
        {"北京": 120, "上海": 120, "杭州": 120}
    )
    assert before == {path: file_sha256(path) for path in v2_paths}


def test_v3_nonblind_oracles_are_byte_equivalent_and_blind_inputs_stay_label_free() -> None:
    nonblind, blind, _nonblind_materializations, _blind_materializations = _build()
    v2_nonblind = {row["case_id"]: row for row in load_jsonl(NONBLIND_PATH_V2)}

    for row in nonblind:
        source = v2_nonblind[row["case_id"]]
        assert row["oracle"] == source["oracle"]
        assert row["oracle_sha256"] == source["oracle_sha256"] == digest(source["oracle"])
    forbidden = {"oracle", "oracle_sha256", "expected", "answer", "ground_truth", "human_label"}
    for row in blind:
        assert not forbidden.intersection(row)
        assert not forbidden.intersection(str(key).casefold() for key in row)


def test_v3_binds_171_unique_historical_ocr_receipts_without_claiming_fresh_execution() -> None:
    nonblind, blind, nonblind_materializations, blind_materializations = _build()
    cases = [*nonblind, *blind]
    materializations = [*nonblind_materializations, *blind_materializations]
    screenshots = [row for row in cases if row["input_kind"] == "SYNTHETIC_SCREENSHOT"]
    screenshot_materializations = [
        row for row in materializations if row["ocr_baseline_receipt"] is not None
    ]

    assert Counter(row["split"] for row in screenshots) == Counter(
        {"dev": 90, "regression": 36, "frozen_blind": 45}
    )
    assert len(screenshot_materializations) == 171
    assert len({row["render_receipt"]["image_sha256"] for row in screenshot_materializations}) == 171
    for case in screenshots:
        binding = case["materialization"]
        provenance = case["provenance"]
        assert provenance["actual_ocr_materialization"] == "PASS_HISTORICAL_V2_RECEIPT"
        assert provenance["v3_receipt_rebinding"] == "PASS"
        assert provenance["fresh_actual_ocr_execution"] == "NOT_RUN"
        assert binding["ocr_source_binding"]["historical_render_receipt_sha256"] == binding[
            "render_receipt"
        ]["content_sha256"]
        assert binding["ocr_source_binding"]["historical_ocr_receipt_sha256"] == binding[
            "ocr_baseline_receipt"
        ]["content_sha256"]
        assert binding["ocr_source_binding"]["historical_cleanup_receipt_sha256"] == binding[
            "cleanup_receipt"
        ]["content_sha256"]


def test_v3_materializer_projection_never_reads_oracle_fields() -> None:
    payload = load_jsonl(NONBLIND_PATH_V2)[0]
    source_materialization = load_jsonl(NONBLIND_MATERIALIZATIONS_PATH_V2)[0]

    class LabelGuard(Mapping[str, object]):
        def __getitem__(self, key: str) -> object:
            if key in {"oracle", "oracle_sha256", "expected"}:
                raise AssertionError(f"label field was accessed: {key}")
            return payload[key]

        def __iter__(self) -> Iterator[str]:
            raise AssertionError("projection must not iterate the case payload")

        def __len__(self) -> int:
            return len(payload)

    projected = materialization_input_projection_v3(LabelGuard(), source_materialization)

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


def test_v3_inner_projection_rejects_rehashed_evidence_tamper() -> None:
    _nonblind, _blind, materializations, _blind_materializations = _build()
    tampered = deepcopy(materializations[0])
    tampered["source_payload"]["city"] = "上海"
    tampered["source_payload"]["content_sha256"] = digest(
        {
            key: value
            for key, value in tampered["source_payload"].items()
            if key != "content_sha256"
        }
    )
    inner_fields = {
        key: deepcopy(tampered[key])
        for key in (
            "case_id",
            "source_payload",
            "provider_snapshot",
            "evidence_snapshot",
            "render_receipt",
            "ocr_baseline_receipt",
            "cleanup_receipt",
            "candidate_sets",
            "receipts",
        )
    }
    inner_fields["schema_version"] = "trip-check-p5-evidence-materialization-v3"
    tampered["evidence_materialization_hash"] = digest(inner_fields)

    with pytest.raises(ValueError):
        evidence_projection_v3(tampered)


def test_v2_source_anchor_is_ready_and_sealed() -> None:
    anchor = validate_v2_source_anchor()

    assert anchor["dataset_id"] == "trip-check-p5-360-v2"
    assert len(anchor["candidate_freeze_commit"]) == 40
    assert anchor["manifest_file_sha256"] == file_sha256(MANIFEST_PATH_V2)
    assert anchor["blind_seal_file_sha256"] == file_sha256(BLIND_SEAL_PATH_V2)


def test_all_360_outer_materializations_are_strictly_case_bound() -> None:
    nonblind, blind, nonblind_materializations, blind_materializations = _build()

    for case_row, materialization in zip(
        [*nonblind, *blind],
        [*nonblind_materializations, *blind_materializations],
        strict=True,
    ):
        case = P5CaseV3.model_validate(case_row)
        assert validate_materialization_v3(case, materialization) == materialization


def test_outer_materialization_rejects_independently_rehashed_ocr_source_binding() -> None:
    nonblind, _blind, materializations, _blind_materializations = _build()
    index = next(
        index for index, row in enumerate(materializations) if row["ocr_source_binding"] is not None
    )
    case = P5CaseV3.model_validate(nonblind[index])
    tampered = deepcopy(materializations[index])
    tampered["ocr_source_binding"]["source_materialization_hash"] = "f" * 64
    tampered["materialization_hash"] = digest(
        {key: value for key, value in tampered.items() if key != "materialization_hash"}
    )

    with pytest.raises(ValueError, match="binding mismatch"):
        validate_materialization_v3(case, tampered)


@pytest.mark.parametrize(
    "fake_commitment",
    [
        {},
        {"status": "NOT_RUN"},
        {"candidate_freeze_commit": "bad"},
    ],
)
def test_manifest_cannot_promote_an_unvalidated_sealing_commitment(fake_commitment) -> None:
    nonblind, blind, nonblind_materializations, blind_materializations = _build()

    with pytest.raises(ValueError):
        build_manifest_v3(
            nonblind_cases=nonblind,
            blind_cases=blind,
            nonblind_materializations=nonblind_materializations,
            blind_materializations=blind_materializations,
            sealing_commitment=fake_commitment,
        )
