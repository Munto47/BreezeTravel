from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, model_validator

from app.trip_understanding.amap_place import AmapPlaceResolver
from app.trip_understanding.full_text import build_full_text_pipeline
from app.trip_understanding.models import PlaceResolutionOutcome
from app.trip_understanding.qwen_provider import QwenStructuredInferenceProvider
from evals.agent_gate_v1.contracts import AgentTaskAttestation
from evals.agent_gate_v1.path_security import (
    read_external_snapshot,
    write_external_bytes_exclusive,
)
from evals.trip_text_cards_agent_v2.contracts import (
    AgentCaseAnnotation,
    AgentCanonicalPlaceLabel,
    AgentDestinationPrediction,
    AgentInferenceCaseOutputV2,
    AgentPredictionRunEnvelope,
    ProviderReceiptRef,
    ProviderRuntimeEffectReceipt,
    SealedAgentReferenceAttestation,
    SealedAgentReferenceBundle,
    StrictModel,
    validate_agent_case_annotation,
)
from evals.trip_text_cards_v1.contracts import (
    TextCardInputCase,
    TextCardPrediction,
    PredictedMention,
    canonical_sha256,
)
from scripts.run_qwen_model_predictions import _candidate, _price, _primary_binding
from scripts.export_g01_amap_live_receipts import (
    build_source_only_catalog,
    extract_source_place_candidates,
)


BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = BACKEND_ROOT.parent
FROZEN_INPUTS = (
    BACKEND_ROOT / "eval_data/trip_text_cards_v1/frozen_blind.inputs.jsonl"
)
MODEL_PANEL = (
    BACKEND_ROOT / "eval_data/trip_text_cards_agent_v2/qwen_model_panel.json"
)
MODEL_BINDING = (
    BACKEND_ROOT / "eval_data/trip_text_cards_agent_v2/provider_binding.json"
)
REFERENCE_SCHEMA = (
    BACKEND_ROOT / "eval_data/trip_text_cards_agent_v2/sealed_agent_reference.schema.json"
)
REFERENCE_DRAFT_PROMPT = (
    BACKEND_ROOT / "eval_data/trip_text_cards_agent_v2/prompts/reference.md"
)
REFERENCE_ADJUDICATION_PROMPT = (
    BACKEND_ROOT / "eval_data/trip_text_cards_agent_v2/prompts/adjudication.md"
)
CANONICAL_REF = "refs/heads/codex/g07-candidate"
CANONICAL_ORIGIN = "https://github.com/Munto47/BreezeTravel.git"
FROZEN_INPUT_SHA256 = "a1d98c44ba5626513546c0c525e335623acbac1ef7f9e1d4431995131c602008"
_ENV_KEYS = {"AMAP_API_KEY", "QWEN_API_KEY", "QWEN_API_URL"}
_FORBIDDEN_KEYS = {
    "api_key",
    "authorization",
    "credential",
    "raw_request",
    "raw_response",
    "secret",
    "token",
}


class SealedCandidateError(ValueError):
    pass


class _FrozenCatalogResolver:
    def __init__(
        self, outcomes: dict[tuple[str, str], PlaceResolutionOutcome]
    ) -> None:
        self.outcomes = outcomes

    async def resolve(
        self,
        *,
        city: str,
        atomic_place_name: str,
        category_hint: str | None = None,
    ) -> PlaceResolutionOutcome:
        del category_hint
        try:
            return self.outcomes[(city, atomic_place_name)]
        except KeyError as exc:
            raise SealedCandidateError(
                "candidate requested a place absent from the role-neutral catalog"
            ) from exc


class SealedReferenceDraft(StrictModel):
    schema_version: Literal["g07-sealed-reference-draft-v1"] = (
        "g07-sealed-reference-draft-v1"
    )
    candidate_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    candidate_tree: str = Field(pattern=r"^[0-9a-f]{40}$")
    reference_input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    attestation: AgentTaskAttestation
    cases: list[AgentCaseAnnotation] = Field(min_length=18, max_length=18)

    @model_validator(mode="after")
    def is_isolated_xhigh_reference(self) -> "SealedReferenceDraft":
        if self.attestation.reasoning_effort != "xhigh":
            raise ValueError("sealed reference drafts require xhigh")
        if (
            self.attestation.subject_commit != self.candidate_commit
            or self.attestation.subject_tree != self.candidate_tree
            or self.attestation.input_bundle_sha256 != self.reference_input_sha256
        ):
            raise ValueError("sealed reference draft attestation drifted")
        return self


class SealedReferenceAdjudication(StrictModel):
    schema_version: Literal["g07-sealed-reference-adjudication-v1"] = (
        "g07-sealed-reference-adjudication-v1"
    )
    candidate_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    candidate_tree: str = Field(pattern=r"^[0-9a-f]{40}$")
    reference_input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_reference_sha256: list[str] = Field(min_length=2, max_length=2)
    resolved_conflict_case_ids: list[str]
    attestation: AgentTaskAttestation
    cases: list[AgentCaseAnnotation] = Field(min_length=18, max_length=18)

    @model_validator(mode="after")
    def is_fresh_ultra_adjudication(self) -> "SealedReferenceAdjudication":
        if len(set(self.source_reference_sha256)) != 2:
            raise ValueError("sealed adjudication requires two distinct references")
        if self.attestation.reasoning_effort != "ultra":
            raise ValueError("sealed adjudication requires ultra")
        if (
            self.attestation.subject_commit != self.candidate_commit
            or self.attestation.subject_tree != self.candidate_tree
            or self.attestation.input_bundle_sha256 != self.reference_input_sha256
        ):
            raise ValueError("sealed adjudication attestation drifted")
        return self


def _git(root: Path, *args: str, timeout: int = 120) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise SealedCandidateError(f"Git sealed binding failed: {' '.join(args)}")
    return result.stdout.strip()


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _jsonl_bytes(values: list[Any]) -> bytes:
    return b"".join(
        (
            json.dumps(
                value.model_dump(mode="json")
                if hasattr(value, "model_dump")
                else value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        for value in values
    )


def _clean_remote_subject(root: Path) -> tuple[str, str]:
    if _git(root, "status", "--porcelain=v1", "--untracked-files=all"):
        raise SealedCandidateError("sealed candidate checkout is not clean")
    if _git(root, "remote", "get-url", "origin") != CANONICAL_ORIGIN:
        raise SealedCandidateError("sealed candidate origin is not canonical")
    commit = _git(root, "rev-parse", "HEAD")
    tree = _git(root, "show", "-s", "--format=%T", commit)
    lines = _git(root, "ls-remote", "--refs", "origin", CANONICAL_REF).splitlines()
    if len(lines) != 1 or lines[0].split(maxsplit=1) != [commit, CANONICAL_REF]:
        raise SealedCandidateError("sealed candidate remote subject drifted")
    return commit, tree


def _load_env(path: Path) -> dict[str, str]:
    snapshot = read_external_snapshot(path, REPOSITORY_ROOT)
    values: dict[str, str] = {}
    try:
        lines = snapshot.content.decode("utf-8-sig").splitlines()
    except UnicodeDecodeError as exc:
        raise SealedCandidateError("sealed credential file is not UTF-8") from exc
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", maxsplit=1)
        key = key.strip()
        if key in _ENV_KEYS:
            values[key] = value.strip().strip("\"'")
    missing = sorted(key for key in _ENV_KEYS if not values.get(key))
    if missing:
        raise SealedCandidateError(
            f"sealed candidate credentials are incomplete: {','.join(missing)}"
        )
    return values


def _load_inputs(raw: bytes) -> list[TextCardInputCase]:
    if _sha256(raw) != FROZEN_INPUT_SHA256:
        raise SealedCandidateError("sealed frozen input byte binding drifted")
    try:
        cases = [
            TextCardInputCase.model_validate_json(line)
            for line in raw.decode("utf-8").splitlines()
            if line.strip()
        ]
    except (UnicodeDecodeError, ValueError) as exc:
        raise SealedCandidateError("sealed frozen inputs are invalid") from exc
    if (
        len(cases) != 18
        or any(case.split != "frozen_blind" for case in cases)
        or len({case.case_id for case in cases}) != 18
    ):
        raise SealedCandidateError("sealed frozen input coverage is invalid")
    return cases


def _walk_forbidden(value: object) -> set[str]:
    if isinstance(value, dict):
        keys = {
            str(key).casefold()
            for key in value
            if str(key).casefold() in _FORBIDDEN_KEYS
        }
        return keys | set().union(
            *(_walk_forbidden(child) for child in value.values()), set()
        )
    if isinstance(value, list):
        return set().union(*(_walk_forbidden(child) for child in value), set())
    return set()


def _destination_prediction(
    case: TextCardInputCase, destination_name: str, destination_basis: str
) -> AgentDestinationPrediction:
    if destination_basis == "EXPLICIT":
        start = case.input_text.find(destination_name)
        if start < 0:
            raise SealedCandidateError("explicit destination is not source-verbatim")
        return AgentDestinationPrediction(
            case_id=case.case_id,
            destination_name=destination_name,
            destination_basis="EXPLICIT",
            evidence_span_start=start,
            evidence_span_end=start + len(destination_name),
            evidence_raw_text=destination_name,
        )
    return AgentDestinationPrediction(
        case_id=case.case_id,
        destination_name=destination_name,
        destination_basis="SOFT_ASSUMPTION",
    )


def _reference_provider_fact(
    *,
    candidate_commit: str,
    city: str,
    source_name: str,
    outcome: PlaceResolutionOutcome,
    provider_binding_sha256: str,
    captured_at: datetime,
) -> dict[str, Any]:
    raw_receipt = outcome.receipt
    matched = outcome.place is not None
    compatible = raw_receipt.get("category_compatible_candidate_count")
    resolution_status = (
        "MATCHED"
        if matched
        else "AMBIGUOUS"
        if isinstance(compatible, int) and compatible > 1
        else "UNRESOLVED"
    )
    observed_raw = raw_receipt.get("observed_at")
    try:
        observed_at = (
            datetime.fromisoformat(str(observed_raw).replace("Z", "+00:00"))
            if observed_raw
            else captured_at
        )
    except ValueError:
        observed_at = captured_at
    latency = raw_receipt.get("latency_ms")
    latency_ms = float(latency) if isinstance(latency, int | float) else 0.0
    started_at = observed_at
    completed_at = max(captured_at, observed_at + timedelta(milliseconds=latency_ms))
    request_sha256 = raw_receipt.get("request_sha256")
    if not isinstance(request_sha256, str) or len(request_sha256) != 64:
        request_sha256 = canonical_sha256(
            {
                "provider": "AMAP_POI_V2",
                "city": city,
                "source_name": source_name,
                "external_calls": raw_receipt.get("external_calls", 0),
            }
        )
    response_sha256 = raw_receipt.get("response_sha256")
    if not isinstance(response_sha256, str) or len(response_sha256) != 64:
        response_sha256 = canonical_sha256(raw_receipt)
    identity = hashlib.sha256(
        f"{candidate_commit}\0{city}\0{source_name}".encode("utf-8")
    ).hexdigest()[:24]
    effect_id = f"g07-sealed-amap-effect-{identity}"
    runtime = ProviderRuntimeEffectReceipt(
        effect_id=effect_id,
        effect_key_sha256=canonical_sha256(
            {
                "city": city,
                "source_name": source_name,
                "provider_binding_sha256": provider_binding_sha256,
            }
        ),
        execution_mode="LIVE",
        provider_binding_sha256=provider_binding_sha256,
        request_sha256=request_sha256,
        response_sha256=response_sha256,
        resolution_status=resolution_status,
        queried_source_name=source_name,
        queried_city=city,
        external_call_count=int(raw_receipt.get("external_calls", 0)),
        place_id=outcome.place.canonical_place_id if matched else None,
        name=outcome.place.name if matched else None,
        city=city if matched else None,
        category=outcome.place.category if matched else None,
        accepted_source_names=[source_name] if matched else [],
        started_at=started_at,
        completed_at=completed_at,
    )
    receipt = ProviderReceiptRef(
        receipt_id=f"g07-sealed-amap-receipt-{identity}",
        execution_mode="LIVE",
        provider_binding_sha256=provider_binding_sha256,
        receipt_ref=effect_id,
        runtime_effect_id=effect_id,
        runtime_effect_receipt_sha256=canonical_sha256(
            runtime.model_dump(mode="json")
        ),
        request_sha256=request_sha256,
        response_sha256=response_sha256,
        observed_at=completed_at,
        resolution_status=resolution_status,
        queried_source_name=source_name,
        queried_city=city,
        accepted_source_name=source_name if matched else None,
    )
    canonical_place = (
        AgentCanonicalPlaceLabel(
            place_id=outcome.place.canonical_place_id,
            name=outcome.place.name,
            city=city,
            category=outcome.place.category,
            provider_receipt=receipt,
        )
        if matched
        else None
    )
    return {
        "queried_city": city,
        "queried_source_name": source_name,
        "query_is_role_neutral": True,
        "provider_runtime_effect": runtime.model_dump(mode="json"),
        "provider_receipt": receipt.model_dump(mode="json"),
        "canonical_place": (
            canonical_place.model_dump(mode="json")
            if canonical_place is not None
            else None
        ),
    }


def _inference_output(case: TextCardInputCase, output: Any) -> AgentInferenceCaseOutputV2:
    mentions: list[PredictedMention] = []
    provider_effects: list[dict[str, Any]] = []
    for activity in output.activities:
        mention = activity.compiled.mention
        matched = activity.place is not None
        binding = activity.place.provider_binding if matched else activity.resolver_receipt
        canonical_city = None
        canonical_category = None
        if matched:
            canonical_city = binding.get("selected_city") or binding.get("city")
            canonical_category = activity.place.category
            if not isinstance(canonical_city, str):
                raise SealedCandidateError("matched place lost canonical city")
        mentions.append(
            PredictedMention(
                span_start=mention.span_start,
                span_end=mention.span_end,
                raw_text=mention.raw_text,
                role=mention.role.value,
                day_index=mention.day_index,
                atomic_place_name=mention.atomic_place_name,
                eligible_for_place_search=activity.compiled.eligible_for_place_search,
                resolution_status=activity.resolution_status.value,
                canonical_place_id=(
                    activity.place.canonical_place_id if matched else None
                ),
                canonical_city=canonical_city,
                canonical_category=canonical_category,
            )
        )
        provider_effects.append(
            {
                "raw_text": mention.raw_text,
                "span_start": mention.span_start,
                "span_end": mention.span_end,
                "role": mention.role.value,
                "day_index": mention.day_index,
                "atomic_place_name": mention.atomic_place_name,
                "eligible_for_place_search": (
                    activity.compiled.eligible_for_place_search
                ),
                "resolution_status": activity.resolution_status.value,
                "place": (
                    activity.place.model_dump(mode="json") if matched else None
                ),
                "resolver_receipt": activity.resolver_receipt,
            }
        )
    provider_binding = {
        "execution_mode": "LIVE",
        "inference_binding": output.inference_binding,
        "resolution_receipt": output.resolution_receipt,
        "provider_effects": provider_effects,
        "provider_effects_sha256": canonical_sha256(provider_effects),
        "raw_request_or_response_retained": False,
    }
    if _walk_forbidden(provider_binding):
        raise SealedCandidateError("sealed prediction contains a forbidden raw field")
    prediction = TextCardPrediction(
        case_id=case.case_id,
        source_sha256=case.normalized_input_sha256,
        destination_name=str(output.destination["name"]),
        provider_binding=provider_binding,
        mentions=mentions,
        public_result=output.public_result.model_dump(mode="json"),
        measurement_scope="LOCAL_PIPELINE_ONLY",
    )
    return AgentInferenceCaseOutputV2(
        case_id=case.case_id,
        source_sha256=case.normalized_input_sha256,
        text_card_prediction=prediction,
        destination_prediction=_destination_prediction(
            case,
            str(output.destination["name"]),
            str(output.destination["status"]),
        ),
    )


async def capture_sealed_candidate(
    *, repository_root: Path, output_root: Path, env_file: Path
) -> dict[str, Path]:
    root = repository_root.resolve(strict=True)
    if root != REPOSITORY_ROOT.resolve(strict=True):
        raise SealedCandidateError("sealed runner repository root disagrees with its code")
    commit, tree = _clean_remote_subject(root)
    output = output_root.resolve(strict=False)
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise SealedCandidateError("sealed candidate output directory must be empty")
    credentials = _load_env(env_file)
    input_bytes = FROZEN_INPUTS.read_bytes()
    cases = _load_inputs(input_bytes)
    panel_bytes = MODEL_PANEL.read_bytes()
    panel = json.loads(panel_bytes)
    candidate = _candidate(panel, "LOW_LATENCY_CANDIDATE")
    model = candidate.get("exact_model_id")
    if not isinstance(model, str) or model == "NOT_EXPOSED_BY_PROVIDER":
        raise SealedCandidateError("sealed Qwen model is not exact-bound")
    qwen = QwenStructuredInferenceProvider(
        api_key=credentials["QWEN_API_KEY"],
        base_url=credentials["QWEN_API_URL"],
        model=model,
        deadline_seconds=7.0,
        max_output_tokens=768,
        max_concurrency=1,
        input_cny_per_million=_price(candidate, "input_token"),
        output_cny_per_million=_price(candidate, "output_token"),
    )
    amap = AmapPlaceResolver(api_key=credentials["AMAP_API_KEY"])
    provider_binding_sha256 = _sha256(MODEL_BINDING.read_bytes())
    catalog = build_source_only_catalog(cases)
    catalog_outcomes: dict[tuple[str, str], PlaceResolutionOutcome] = {}
    catalog_facts: dict[tuple[str, str], dict[str, Any]] = {}
    amap_calls = 0
    for city, names in catalog.items():
        for name in names:
            outcome = await amap.resolve(city=city, atomic_place_name=name)
            if _walk_forbidden(outcome.model_dump(mode="json")):
                raise SealedCandidateError(
                    "sealed Provider outcome contains a forbidden raw field"
                )
            catalog_outcomes[(city, name)] = outcome
            catalog_facts[(city, name)] = _reference_provider_fact(
                candidate_commit=commit,
                city=city,
                source_name=name,
                outcome=outcome,
                provider_binding_sha256=provider_binding_sha256,
                captured_at=datetime.now(UTC),
            )
            amap_calls += int(outcome.receipt.get("external_calls", 0))
    pipeline = build_full_text_pipeline(
        primary_inference_provider=qwen,
        place_resolver=_FrozenCatalogResolver(catalog_outcomes),
        max_place_concurrency=4,
    )
    combined: list[AgentInferenceCaseOutputV2] = []
    reference_cases: list[dict[str, Any]] = []
    qwen_calls = 0
    for index, case in enumerate(cases, start=1):
        output_value = await pipeline.run(case.input_text)
        primary = _primary_binding(output_value.inference_binding)
        if (
            primary.get("execution_mode") != "LIVE"
            or primary.get("external_calls") != 1
            or primary.get("repair_call_count") != 0
            or primary.get("fallback_used") is True
            or primary.get("raw_request_or_response_retained") is not False
        ):
            raise SealedCandidateError(
                f"sealed Qwen case did not complete exact live inference: {case.case_id}"
            )
        item = _inference_output(case, output_value)
        combined.append(item)
        qwen_calls += int(primary["external_calls"])
        source_names = extract_source_place_candidates(case.input_text)
        reference_cases.append(
            {
                "case_id": case.case_id,
                "source_sha256": case.normalized_input_sha256,
                "provider_effects": [
                    catalog_facts[(city, name)]
                    for city in case.city_scope
                    for name in source_names
                ],
            }
        )
        print(
            json.dumps(
                {"completed": index, "total": 18, "case_id": case.case_id},
                sort_keys=True,
            ),
            flush=True,
        )
    if qwen_calls != 18:
        raise SealedCandidateError("sealed Qwen call count is not exactly 18")
    predictions = [item.text_card_prediction for item in combined]
    prediction_bytes = _jsonl_bytes(predictions)
    combined_bytes = _jsonl_bytes(combined)
    input_copy_path = output / "frozen_blind.inputs.jsonl"
    prediction_path = output / "predictions.jsonl"
    combined_path = output / "inference_outputs.jsonl"
    write_external_bytes_exclusive(input_copy_path, input_bytes, root)
    write_external_bytes_exclusive(prediction_path, prediction_bytes, root)
    write_external_bytes_exclusive(combined_path, combined_bytes, root)
    draft_schema_path = output / "reference_draft.schema.json"
    adjudication_schema_path = output / "reference_adjudication.schema.json"
    write_external_bytes_exclusive(
        draft_schema_path,
        _json_bytes(SealedReferenceDraft.model_json_schema()),
        root,
    )
    write_external_bytes_exclusive(
        adjudication_schema_path,
        _json_bytes(SealedReferenceAdjudication.model_json_schema()),
        root,
    )
    reference_input = {
        "schema_version": "g07-sealed-reference-input-v1",
        "goal_id": "TC-VNEXT-G07-CANDIDATE",
        "candidate_commit": commit,
        "candidate_tree": tree,
        "input_path": str(input_copy_path),
        "input_sha256": _sha256(input_bytes),
        "case_count": 18,
        "provider_binding_sha256": provider_binding_sha256,
        "provider_effect_case_count": 18,
        "provider_external_call_count": amap_calls,
        "cases": reference_cases,
        "reference_prompt_sha256": _sha256(REFERENCE_DRAFT_PROMPT.read_bytes()),
        "adjudication_prompt_sha256": _sha256(
            REFERENCE_ADJUDICATION_PROMPT.read_bytes()
        ),
        "reference_draft_schema_path": str(draft_schema_path),
        "reference_draft_schema_sha256": _sha256(draft_schema_path.read_bytes()),
        "adjudication_schema_path": str(adjudication_schema_path),
        "adjudication_schema_sha256": _sha256(
            adjudication_schema_path.read_bytes()
        ),
        "candidate_predictions_visible": False,
        "raw_provider_response_retained": False,
        "human_evidence": False,
    }
    if _walk_forbidden(reference_input):
        raise SealedCandidateError("sealed reference input contains a forbidden raw field")
    reference_input_path = output / "reference_input.json"
    write_external_bytes_exclusive(
        reference_input_path, _json_bytes(reference_input), root
    )
    runtime = {
        "schema_version": "g07-sealed-runtime-receipt-v1",
        "goal_id": "TC-VNEXT-G07-CANDIDATE",
        "candidate_commit": commit,
        "candidate_tree": tree,
        "input_sha256": _sha256(input_bytes),
        "predictions_sha256": _sha256(prediction_bytes),
        "inference_outputs_sha256": _sha256(combined_bytes),
        "reference_input_sha256": _sha256(reference_input_path.read_bytes()),
        "case_count": 18,
        "exact_model_id": model,
        "qwen_external_call_count": qwen_calls,
        "qwen_repair_call_count": 0,
        "amap_external_call_count": amap_calls,
        "blind_truth_read": 0,
        "raw_request_or_response_retained": False,
        "human_evidence": False,
        "completed_at": datetime.now(UTC).isoformat(),
        "verdict": "CAPTURE_COMPLETE",
    }
    runtime_path = output / "runtime_receipt.json"
    write_external_bytes_exclusive(runtime_path, _json_bytes(runtime), root)
    envelope = AgentPredictionRunEnvelope(
        split="frozen_blind",
        candidate_commit=commit,
        candidate_tree=tree,
        predictions_sha256=_sha256(prediction_bytes),
        inference_outputs_sha256=_sha256(combined_bytes),
        model_binding_sha256=_sha256(panel_bytes),
        prompt_sha256=_sha256(
            (
                BACKEND_ROOT
                / "eval_data/trip_text_cards_agent_v2/qwen_inference_prompt.md"
            ).read_bytes()
        ),
        schema_sha256=_sha256(
            (
                BACKEND_ROOT
                / "eval_data/trip_text_cards_agent_v2/qwen_semantic_draft.schema.json"
            ).read_bytes()
        ),
        config_sha256=_sha256(
            (
                BACKEND_ROOT
                / "eval_data/trip_text_cards_agent_v2/qwen_inference_config.json"
            ).read_bytes()
        ),
        provider_binding_sha256=_sha256(MODEL_BINDING.read_bytes()),
        inference_receipt_bundle_sha256=_sha256(runtime_path.read_bytes()),
        generated_at=datetime.now(UTC),
        destination_predictions=[item.destination_prediction for item in combined],
    )
    envelope_path = output / "prediction_envelope.json"
    write_external_bytes_exclusive(
        envelope_path, _json_bytes(envelope.model_dump(mode="json")), root
    )
    return {
        "sealed.inputs": input_copy_path,
        "sealed.predictions": prediction_path,
        "sealed.inference_outputs": combined_path,
        "sealed.prediction_envelope": envelope_path,
        "sealed.runtime": runtime_path,
        "sealed.reference_input": reference_input_path,
    }


def finalize_sealed_truth(
    *,
    repository_root: Path,
    reference_input_path: Path,
    input_path: Path,
    reference_paths: list[Path],
    adjudication_path: Path,
    output_path: Path,
) -> SealedAgentReferenceBundle:
    root = repository_root.resolve(strict=True)
    commit, tree = _clean_remote_subject(root)
    input_snapshot = read_external_snapshot(input_path, root)
    cases = _load_inputs(input_snapshot.content)
    reference_input = read_external_snapshot(reference_input_path, root)
    try:
        reference_input_value = json.loads(reference_input.content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SealedCandidateError("invalid sealed reference input") from exc
    if not isinstance(reference_input_value, dict):
        raise SealedCandidateError("invalid sealed reference input")
    reference_case_values = reference_input_value.get("cases")
    if (
        reference_input_value.get("schema_version")
        != "g07-sealed-reference-input-v1"
        or reference_input_value.get("case_count") != 18
        or reference_input_value.get("provider_effect_case_count") != 18
        or not isinstance(reference_case_values, list)
        or [
            item.get("case_id")
            for item in reference_case_values
            if isinstance(item, dict)
        ]
        != [case.case_id for case in cases]
    ):
        raise SealedCandidateError("sealed reference input coverage is invalid")

    provider_facts_by_case: dict[str, dict[tuple[str, str], dict[str, Any]]] = {}
    unique_runtime_effects: dict[str, ProviderRuntimeEffectReceipt] = {}
    try:
        for source_case, case_value in zip(cases, reference_case_values, strict=True):
            if not isinstance(case_value, dict):
                raise ValueError("reference case is not an object")
            if case_value.get("source_sha256") != source_case.normalized_input_sha256:
                raise ValueError("reference case source drifted")
            facts: dict[tuple[str, str], dict[str, Any]] = {}
            effects = case_value.get("provider_effects")
            if not isinstance(effects, list):
                raise ValueError("provider effects are not a list")
            for effect in effects:
                if (
                    not isinstance(effect, dict)
                    or effect.get("query_is_role_neutral") is not True
                ):
                    raise ValueError("provider effect is not role neutral")
                receipt = ProviderReceiptRef.model_validate(
                    effect.get("provider_receipt")
                )
                runtime = ProviderRuntimeEffectReceipt.model_validate(
                    effect.get("provider_runtime_effect")
                )
                canonical_value = effect.get("canonical_place")
                canonical_place = (
                    AgentCanonicalPlaceLabel.model_validate(canonical_value)
                    if canonical_value is not None
                    else None
                )
                if (
                    receipt.runtime_effect_id != runtime.effect_id
                    or receipt.runtime_effect_receipt_sha256
                    != canonical_sha256(runtime.model_dump(mode="json"))
                    or receipt.provider_binding_sha256
                    != runtime.provider_binding_sha256
                    or receipt.request_sha256 != runtime.request_sha256
                    or receipt.response_sha256 != runtime.response_sha256
                    or receipt.resolution_status != runtime.resolution_status
                    or receipt.queried_city != runtime.queried_city
                    or receipt.queried_source_name != runtime.queried_source_name
                    or receipt.observed_at != runtime.completed_at
                    or receipt.accepted_source_name
                    != (
                        receipt.queried_source_name
                        if runtime.resolution_status == "MATCHED"
                        else None
                    )
                    or receipt.queried_city != effect.get("queried_city")
                    or receipt.queried_source_name
                    != effect.get("queried_source_name")
                    or (canonical_place is None)
                    != (receipt.resolution_status != "MATCHED")
                    or (
                        canonical_place is not None
                        and (
                            canonical_place.provider_receipt != receipt
                            or canonical_place.place_id != runtime.place_id
                            or canonical_place.name != runtime.name
                            or canonical_place.city != runtime.city
                            or canonical_place.category != runtime.category
                        )
                    )
                ):
                    raise ValueError("provider reference fact drifted")
                prior = unique_runtime_effects.setdefault(runtime.effect_id, runtime)
                if prior != runtime:
                    raise ValueError("Provider runtime effect is inconsistent")
                key = (receipt.queried_city, receipt.queried_source_name)
                if key in facts:
                    raise ValueError("duplicate provider reference fact")
                facts[key] = {
                    "provider_receipt": receipt.model_dump(mode="json"),
                    "canonical_place": (
                        canonical_place.model_dump(mode="json")
                        if canonical_place is not None
                        else None
                    ),
                }
            provider_facts_by_case[str(case_value["case_id"])] = facts
        if (
            reference_input_value.get("provider_binding_sha256")
            != _sha256(MODEL_BINDING.read_bytes())
            or sum(
                effect.external_call_count
                for effect in unique_runtime_effects.values()
            )
            != reference_input_value.get("provider_external_call_count")
        ):
            raise ValueError("Provider reference aggregate drifted")
    except (KeyError, TypeError, ValueError) as exc:
        raise SealedCandidateError(
            "sealed reference Provider facts are invalid"
        ) from exc

    source_by_id = {case.case_id: case for case in cases}

    def validate_reference_cases(values: list[AgentCaseAnnotation]) -> None:
        for case_value in values:
            validate_agent_case_annotation(
                case_value, source_by_id[case_value.case_id]
            )
            allowed = provider_facts_by_case[case_value.case_id]
            for mention in case_value.mentions:
                if not mention.executable_place:
                    continue
                receipt = mention.provider_resolution_receipt
                if receipt is None:
                    raise ValueError("executable place lost its Provider receipt")
                expected = allowed.get(
                    (receipt.queried_city, receipt.queried_source_name)
                )
                actual_canonical = (
                    mention.canonical_place.model_dump(mode="json")
                    if mention.canonical_place is not None
                    else None
                )
                if (
                    expected is None
                    or receipt.model_dump(mode="json")
                    != expected["provider_receipt"]
                    or actual_canonical != expected["canonical_place"]
                ):
                    raise ValueError("reference used an unbound Provider fact")
    if len(reference_paths) != 2:
        raise SealedCandidateError("sealed truth requires exactly two references")
    reference_snapshots = [
        read_external_snapshot(path, root) for path in reference_paths
    ]
    try:
        references = [
            SealedReferenceDraft.model_validate_json(snapshot.content)
            for snapshot in reference_snapshots
        ]
        adjudication_snapshot = read_external_snapshot(adjudication_path, root)
        adjudication = SealedReferenceAdjudication.model_validate_json(
            adjudication_snapshot.content
        )
    except ValueError as exc:
        raise SealedCandidateError("invalid sealed reference artifact") from exc
    expected_ids = [case.case_id for case in cases]
    for reference in references:
        if (
            reference.candidate_commit != commit
            or reference.candidate_tree != tree
            or reference.reference_input_sha256 != reference_input.sha256
            or [case.case_id for case in reference.cases] != expected_ids
        ):
            raise SealedCandidateError("sealed reference candidate binding mismatch")
        if (
            reference.attestation.prompt_sha256
            != reference_input_value.get("reference_prompt_sha256")
            or reference.attestation.output_schema_sha256
            != reference_input_value.get("reference_draft_schema_sha256")
        ):
            raise SealedCandidateError("sealed reference prompt/schema drifted")
        try:
            validate_reference_cases(reference.cases)
        except ValueError as exc:
            raise SealedCandidateError("sealed reference truth is invalid") from exc
    if (
        reference_input_value.get("candidate_commit") != commit
        or reference_input_value.get("candidate_tree") != tree
        or reference_input_value.get("input_sha256") != input_snapshot.sha256
        or reference_input_value.get("candidate_predictions_visible") is not False
    ):
        raise SealedCandidateError("sealed reference input binding mismatch")
    if references[0].attestation.task_id == references[1].attestation.task_id:
        raise SealedCandidateError("sealed references must use distinct tasks")
    reference_hashes = sorted(snapshot.sha256 for snapshot in reference_snapshots)
    conflicts = sorted(
        first.case_id
        for first, second in zip(
            references[0].cases, references[1].cases, strict=True
        )
        if canonical_sha256(first.model_dump(mode="json"))
        != canonical_sha256(second.model_dump(mode="json"))
    )
    if (
        adjudication.candidate_commit != commit
        or adjudication.candidate_tree != tree
        or adjudication.reference_input_sha256 != reference_input.sha256
        or sorted(adjudication.source_reference_sha256) != reference_hashes
        or sorted(adjudication.resolved_conflict_case_ids) != conflicts
        or [case.case_id for case in adjudication.cases] != expected_ids
        or adjudication.attestation.task_id
        in {item.attestation.task_id for item in references}
        or adjudication.attestation.started_at
        < max(item.attestation.frozen_at for item in references)
        or adjudication.attestation.prompt_sha256
        != reference_input_value.get("adjudication_prompt_sha256")
        or adjudication.attestation.output_schema_sha256
        != reference_input_value.get("adjudication_schema_sha256")
    ):
        raise SealedCandidateError("sealed adjudication binding mismatch")
    try:
        validate_reference_cases(adjudication.cases)
    except ValueError as exc:
        raise SealedCandidateError("sealed adjudication truth is invalid") from exc
    attestation_values = adjudication.attestation.model_dump(mode="json")
    attestation_values["output_schema_sha256"] = _sha256(
        REFERENCE_SCHEMA.read_bytes()
    )
    attestation = SealedAgentReferenceAttestation(
        **attestation_values,
        task_role="SEALED_REFERENCE_CUSTODIAN",
        saw_candidate_predictions_before_submission=False,
        candidate_output_visibility="NONE",
        raw_output_storage="REPOSITORY_EXTERNAL",
        provider_receipts_used=True,
    )
    truth = SealedAgentReferenceBundle(
        attestation=attestation,
        agent_reference_cases=adjudication.cases,
        human_evidence=False,
    )
    write_external_bytes_exclusive(
        output_path,
        _json_bytes(truth.model_dump(mode="json")),
        root,
    )
    return truth


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    capture = subparsers.add_parser("capture")
    capture.add_argument("--repository-root", required=True, type=Path)
    capture.add_argument("--output-root", required=True, type=Path)
    capture.add_argument("--env-file", required=True, type=Path)
    finalize = subparsers.add_parser("finalize-truth")
    finalize.add_argument("--repository-root", required=True, type=Path)
    finalize.add_argument("--reference-input", required=True, type=Path)
    finalize.add_argument("--inputs", required=True, type=Path)
    finalize.add_argument("--reference", action="append", required=True, type=Path)
    finalize.add_argument("--adjudication", required=True, type=Path)
    finalize.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.command == "capture":
            artifacts = asyncio.run(
                capture_sealed_candidate(
                    repository_root=args.repository_root,
                    output_root=args.output_root,
                    env_file=args.env_file,
                )
            )
            print(
                json.dumps(
                    {"status": "CAPTURE_COMPLETE", "artifacts": artifacts},
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                )
            )
        else:
            truth = finalize_sealed_truth(
                repository_root=args.repository_root,
                reference_input_path=args.reference_input,
                input_path=args.inputs,
                reference_paths=args.reference,
                adjudication_path=args.adjudication,
                output_path=args.output,
            )
            print(
                json.dumps(
                    {
                        "status": "TRUTH_FROZEN",
                        "case_count": len(truth.agent_reference_cases),
                        "human_evidence": False,
                        "output": str(args.output.resolve()),
                    },
                    sort_keys=True,
                )
            )
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
