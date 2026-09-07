from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.trip_understanding.models import PlaceResolutionOutcome, ResolvedPlace
from evals.trip_text_cards_agent_v2.contracts import AgentCaseAnnotation
from scripts import run_g07_sealed_candidate as sealed
from scripts.run_g07_sealed_candidate import (
    SealedCandidateError,
    SealedReferenceAdjudication,
    SealedReferenceDraft,
    _load_inputs,
    _reference_provider_fact,
    finalize_sealed_truth,
)


ROOT = Path(__file__).resolve().parents[2]
COMMIT = "1" * 40
TREE = "2" * 40


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _minimal_cases() -> list[AgentCaseAnnotation]:
    cases = _load_inputs(sealed.FROZEN_INPUTS.read_bytes())
    values: list[AgentCaseAnnotation] = []
    for source in cases:
        destination = source.city_scope[0]
        destination_start = source.input_text.index(destination)
        raw = source.input_text[0]
        values.append(
            AgentCaseAnnotation(
                case_id=source.case_id,
                source_sha256=source.normalized_input_sha256,
                destination_name=destination,
                destination_basis="EXPLICIT",
                destination_evidence_span_start=destination_start,
                destination_evidence_span_end=destination_start
                + len(destination),
                destination_evidence_raw_text=destination,
                mentions=[
                    {
                        "mention_id": f"{source.case_id}-minimal",
                        "span_start": 0,
                        "span_end": len(raw),
                        "raw_text": raw,
                        "semantic_kind": "OTHER",
                        "role": "REFERENCE",
                        "day_index": None,
                        "place_boundary_status": "NONE",
                        "place_boundary_basis": "NONE",
                        "atomic_place_name": None,
                        "executable_place": False,
                        "provider_resolution_receipt": None,
                        "canonical_place": None,
                    }
                ],
            )
        )
    return values


def _attestation(
    *,
    task_id: str,
    reasoning_effort: str,
    input_sha256: str,
    prompt_path: Path,
    output_schema_sha256: str,
    started_at: datetime,
) -> dict[str, object]:
    completed = started_at + timedelta(seconds=1)
    return {
        "task_id": task_id,
        "model": "gpt-5.6-sol",
        "reasoning_effort": reasoning_effort,
        "prompt_sha256": hashlib.sha256(prompt_path.read_bytes()).hexdigest(),
        "input_bundle_sha256": input_sha256,
        "output_schema_sha256": output_schema_sha256,
        "subject_commit": COMMIT,
        "subject_tree": TREE,
        "started_at": started_at.isoformat(),
        "completed_at": completed.isoformat(),
        "frozen_at": completed.isoformat(),
        "context_fork": "none",
        "isolated_context": True,
        "human_evidence": False,
        "saw_prior_verdict": False,
    }


def _truth_artifacts(
    tmp_path: Path,
) -> tuple[Path, Path, list[Path], Path, list[AgentCaseAnnotation]]:
    inputs = tmp_path / "inputs.jsonl"
    inputs.write_bytes(sealed.FROZEN_INPUTS.read_bytes())
    draft_schema_sha256 = "3" * 64
    reference_input = tmp_path / "reference-input.json"
    reference_input_value = {
        "schema_version": "g07-sealed-reference-input-v1",
        "candidate_commit": COMMIT,
        "candidate_tree": TREE,
        "input_sha256": hashlib.sha256(inputs.read_bytes()).hexdigest(),
        "case_count": 18,
        "provider_effect_case_count": 18,
        "provider_binding_sha256": hashlib.sha256(
            sealed.MODEL_BINDING.read_bytes()
        ).hexdigest(),
        "provider_external_call_count": 0,
        "cases": [
            {
                "case_id": case.case_id,
                "source_sha256": case.source_sha256,
                "provider_effects": [],
            }
            for case in _minimal_cases()
        ],
        "candidate_predictions_visible": False,
        "reference_prompt_sha256": hashlib.sha256(
            sealed.REFERENCE_DRAFT_PROMPT.read_bytes()
        ).hexdigest(),
        "adjudication_prompt_sha256": hashlib.sha256(
            sealed.REFERENCE_ADJUDICATION_PROMPT.read_bytes()
        ).hexdigest(),
        "reference_draft_schema_sha256": draft_schema_sha256,
        "adjudication_schema_sha256": "4" * 64,
    }
    _write_json(reference_input, reference_input_value)
    input_sha256 = hashlib.sha256(reference_input.read_bytes()).hexdigest()
    cases = _minimal_cases()
    started = datetime.now(UTC)
    references: list[Path] = []
    for index in (1, 2):
        path = tmp_path / f"reference-{index}.json"
        value = SealedReferenceDraft(
            candidate_commit=COMMIT,
            candidate_tree=TREE,
            reference_input_sha256=input_sha256,
            attestation=_attestation(
                task_id=f"/sealed/reference-{index}",
                reasoning_effort="xhigh",
                input_sha256=input_sha256,
                prompt_path=sealed.REFERENCE_DRAFT_PROMPT,
                output_schema_sha256=draft_schema_sha256,
                started_at=started,
            ),
            cases=cases,
        )
        _write_json(path, value.model_dump(mode="json"))
        references.append(path)
    adjudication = tmp_path / "adjudication.json"
    source_hashes = [hashlib.sha256(path.read_bytes()).hexdigest() for path in references]
    adjudication_value = SealedReferenceAdjudication(
        candidate_commit=COMMIT,
        candidate_tree=TREE,
        reference_input_sha256=input_sha256,
        source_reference_sha256=source_hashes,
        resolved_conflict_case_ids=[],
        attestation=_attestation(
            task_id="/sealed/adjudication",
            reasoning_effort="ultra",
            input_sha256=input_sha256,
            prompt_path=sealed.REFERENCE_ADJUDICATION_PROMPT,
            output_schema_sha256="4" * 64,
            started_at=started + timedelta(seconds=2),
        ),
        cases=cases,
    )
    _write_json(adjudication, adjudication_value.model_dump(mode="json"))
    return inputs, reference_input, references, adjudication, cases


def test_frozen_inputs_are_exactly_bound_and_cover_18_cases() -> None:
    cases = _load_inputs(sealed.FROZEN_INPUTS.read_bytes())

    assert len(cases) == 18
    assert {case.split for case in cases} == {"frozen_blind"}
    with pytest.raises(SealedCandidateError, match="byte binding"):
        _load_inputs(sealed.FROZEN_INPUTS.read_bytes() + b"\n")


def test_finalize_truth_requires_two_isolated_references_and_fresh_ultra(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs, reference_input, references, adjudication, cases = _truth_artifacts(
        tmp_path
    )
    monkeypatch.setattr(sealed, "_clean_remote_subject", lambda _root: (COMMIT, TREE))

    truth = finalize_sealed_truth(
        repository_root=ROOT,
        reference_input_path=reference_input,
        input_path=inputs,
        reference_paths=references,
        adjudication_path=adjudication,
        output_path=tmp_path / "truth.json",
    )

    assert truth.attestation.reasoning_effort == "ultra"
    assert truth.attestation.candidate_output_visibility == "NONE"
    assert truth.attestation.saw_candidate_predictions_before_submission is False
    assert truth.agent_reference_cases == cases


def test_finalize_truth_rejects_missing_reference_and_source_hash_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs, reference_input, references, adjudication, _cases = _truth_artifacts(
        tmp_path
    )
    monkeypatch.setattr(sealed, "_clean_remote_subject", lambda _root: (COMMIT, TREE))
    with pytest.raises(SealedCandidateError, match="exactly two"):
        finalize_sealed_truth(
            repository_root=ROOT,
            reference_input_path=reference_input,
            input_path=inputs,
            reference_paths=references[:1],
            adjudication_path=adjudication,
            output_path=tmp_path / "missing.json",
        )
    value = json.loads(adjudication.read_text(encoding="utf-8"))
    value["source_reference_sha256"][0] = "0" * 64
    _write_json(adjudication, value)
    with pytest.raises(SealedCandidateError, match="adjudication binding"):
        finalize_sealed_truth(
            repository_root=ROOT,
            reference_input_path=reference_input,
            input_path=inputs,
            reference_paths=references,
            adjudication_path=adjudication,
            output_path=tmp_path / "drifted.json",
        )


def test_reference_provider_fact_is_typed_and_tamper_evident(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs, reference_input, references, adjudication, _cases = _truth_artifacts(
        tmp_path
    )
    observed_at = datetime.now(UTC)
    fact = _reference_provider_fact(
        candidate_commit=COMMIT,
        city="北京",
        source_name="示例地点",
        outcome=PlaceResolutionOutcome(
            place=ResolvedPlace(
                canonical_place_id="provider-place-1",
                name="示例地点",
                category="景点",
                area_or_address="示例地址",
                provider_binding={},
            ),
            receipt={
                "request_sha256": "5" * 64,
                "response_sha256": "6" * 64,
                "observed_at": observed_at.isoformat(),
                "external_calls": 1,
            },
        ),
        provider_binding_sha256=hashlib.sha256(
            sealed.MODEL_BINDING.read_bytes()
        ).hexdigest(),
        captured_at=observed_at,
    )
    value = json.loads(reference_input.read_text(encoding="utf-8"))
    value["provider_binding_sha256"] = hashlib.sha256(
        sealed.MODEL_BINDING.read_bytes()
    ).hexdigest()
    value["provider_external_call_count"] = 1
    value["cases"][0]["provider_effects"] = [fact]
    value["cases"][0]["provider_effects"][0]["provider_receipt"][
        "request_sha256"
    ] = "7" * 64
    _write_json(reference_input, value)
    monkeypatch.setattr(sealed, "_clean_remote_subject", lambda _root: (COMMIT, TREE))

    with pytest.raises(SealedCandidateError, match="Provider facts"):
        finalize_sealed_truth(
            repository_root=ROOT,
            reference_input_path=reference_input,
            input_path=inputs,
            reference_paths=references,
            adjudication_path=adjudication,
            output_path=tmp_path / "tampered.json",
        )
