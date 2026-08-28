from __future__ import annotations

import asyncio
import hashlib
import json
import time
from pathlib import Path
from typing import Annotated, Any, Literal

from openai import AsyncOpenAI
from pydantic import Field, ValidationError, model_validator

from app.trip_understanding.errors import InferenceProviderUnavailableError
from app.trip_understanding.models import (
    ActivityRole,
    DestinationBasis,
    InferenceProposal,
    ProposedMention,
    StrictModel,
)
from app.trip_understanding.pipeline import canonical_sha256


_PROMPT_PATH = (
    Path(__file__).resolve().parents[2]
    / "eval_data"
    / "trip_text_cards_agent_v2"
    / "qwen_inference_prompt.md"
)
_SCHEMA_PATH = _PROMPT_PATH.with_name("qwen_semantic_draft.schema.json")
_CONFIG_PATH = _PROMPT_PATH.with_name("qwen_inference_config.json")
_MODEL_PANEL_PATH = _PROMPT_PATH.with_name("qwen_model_panel.json")
_FORBIDDEN_ATOMIC_MARKERS = ("预约", "说明", "网址", "链接", "http://", "https://")
_SENTENCE_MARKERS = set("。！？；\n")


def _redacted_validation_category(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        categories = []
        for item in exc.errors(include_input=False, include_url=False):
            location = ".".join(str(part) for part in item.get("loc", ()))
            categories.append(f"{item.get('type', 'validation_error')}:{location}")
        return "|".join(sorted(categories))[:500] or "PYDANTIC_VALIDATION_ERROR"
    if isinstance(exc, json.JSONDecodeError):
        return "JSON_DECODE_ERROR"
    message = str(exc)
    if message in {
        "DESTINATION_SPAN_MISMATCH",
        "MENTION_SPAN_OUT_OF_RANGE",
        "DUPLICATE_MENTION_SPAN",
        "ATOMIC_PLACE_SPAN_MISMATCH",
        "FORBIDDEN_ATOMIC_PLACE",
        "NON_ATOMIC_PLACE",
        "EMPTY_STRUCTURED_OUTPUT",
    }:
        return message
    return "VALUE_ERROR"


class QwenExplicitDestinationDraft(StrictModel):
    name: str = Field(min_length=1, max_length=40)
    basis: Literal[DestinationBasis.EXPLICIT]
    evidence_span_start: int = Field(ge=0)
    evidence_span_end: int = Field(gt=0)

    @model_validator(mode="after")
    def evidence_span_is_non_empty(self) -> "QwenExplicitDestinationDraft":
        if self.evidence_span_end <= self.evidence_span_start:
            raise ValueError("destination evidence span is empty")
        return self


class QwenSoftDestinationDraft(StrictModel):
    name: str = Field(min_length=1, max_length=40)
    basis: Literal[DestinationBasis.SOFT_ASSUMPTION]
    evidence_span_start: Literal[None]
    evidence_span_end: Literal[None]


QwenDestinationDraft = Annotated[
    QwenExplicitDestinationDraft | QwenSoftDestinationDraft,
    Field(discriminator="basis"),
]


class QwenMentionDraft(StrictModel):
    span_start: int = Field(ge=0)
    span_end: int = Field(gt=0)
    role: ActivityRole
    day_index: int | None = Field(ge=1, le=14)
    sequence_index: int = Field(ge=0)
    atomic_place_name: str | None = Field(max_length=40)
    category_hint: str | None = Field(max_length=40)
    time_hint: str | None = Field(max_length=80)

    @model_validator(mode="after")
    def span_and_day_are_consistent(self) -> "QwenMentionDraft":
        if self.span_end <= self.span_start:
            raise ValueError("mention span is empty")
        if self.role == ActivityRole.PLANNED and self.day_index is None:
            raise ValueError("planned mention requires a day")
        return self


class QwenSemanticDraft(StrictModel):
    destination: QwenDestinationDraft
    mentions: list[QwenMentionDraft] = Field(max_length=160)


def qwen_semantic_schema() -> dict[str, Any]:
    return QwenSemanticDraft.model_json_schema()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _estimated_cost(
    *,
    input_tokens: int,
    output_tokens: int,
    input_cny_per_million: float | None,
    output_cny_per_million: float | None,
) -> tuple[float | None, str]:
    if input_cny_per_million is None or output_cny_per_million is None:
        return None, "NOT_EXPOSED_BY_PROVIDER"
    value = (
        input_tokens * input_cny_per_million
        + output_tokens * output_cny_per_million
    ) / 1_000_000
    return round(value, 8), "CALCULATED_FROM_PROVIDER_FIELDS"


class QwenStructuredInferenceProvider:
    """Qwen implementation of the model-neutral StructuredInferenceProvider.

    Only redacted hashes and usage metrics leave this object in the returned
    binding. Source text and raw Provider output are deliberately not retained.
    """

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        deadline_seconds: float = 7.0,
        max_output_tokens: int = 2048,
        temperature: float = 0.1,
        input_cny_per_million: float | None = None,
        output_cny_per_million: float | None = None,
        client: Any | None = None,
        prompt: str | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("Qwen API key is required")
        if not base_url.startswith("https://"):
            raise ValueError("Qwen base URL must use HTTPS")
        if not model:
            raise ValueError("an exact Qwen model ID is required")
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.deadline_seconds = deadline_seconds
        self.max_output_tokens = max_output_tokens
        self.temperature = temperature
        self.input_cny_per_million = input_cny_per_million
        self.output_cny_per_million = output_cny_per_million
        prompt_bytes = _PROMPT_PATH.read_bytes()
        self.prompt = (
            prompt if prompt is not None else prompt_bytes.decode("utf-8")
        )
        generated_schema = qwen_semantic_schema()
        stored_schema_bytes = _SCHEMA_PATH.read_bytes()
        stored_schema = json.loads(stored_schema_bytes)
        if canonical_sha256(stored_schema) != canonical_sha256(generated_schema):
            raise ValueError("frozen Qwen schema disagrees with adapter models")
        self.schema = stored_schema
        self.client = client or AsyncOpenAI(
            api_key=api_key,
            base_url=self.base_url,
            timeout=deadline_seconds,
            max_retries=0,
        )
        self.prompt_sha256 = _sha256_text(self.prompt)
        self.prompt_artifact_sha256 = hashlib.sha256(prompt_bytes).hexdigest()
        self.schema_canonical_sha256 = canonical_sha256(self.schema)
        self.schema_artifact_sha256 = hashlib.sha256(stored_schema_bytes).hexdigest()
        self.config_artifact_sha256 = hashlib.sha256(
            _CONFIG_PATH.read_bytes()
        ).hexdigest()
        self.model_panel_sha256 = hashlib.sha256(
            _MODEL_PANEL_PATH.read_bytes()
        ).hexdigest()
        self.effective_config_sha256 = canonical_sha256(
            {
                "thinking": False,
                "temperature": temperature,
                "deadline_ms": round(deadline_seconds * 1000),
                "maximum_repair_calls": 1,
                "max_output_tokens": max_output_tokens,
            }
        )
        self.schema_sha256 = self.schema_artifact_sha256
        self.config_sha256 = self.config_artifact_sha256

    async def _call(
        self,
        *,
        source_text: str,
        attempt: Literal["INITIAL", "SCHEMA_REPAIR"],
        receipt: dict[str, object],
        prior_output: str | None = None,
        validation_category: str | None = None,
    ) -> str:
        source_sha256 = _sha256_text(source_text)
        messages: list[dict[str, str]] = [
            {"role": "system", "content": self.prompt},
            {"role": "user", "content": source_text},
        ]
        if attempt == "SCHEMA_REPAIR":
            messages.extend(
                [
                    {"role": "assistant", "content": prior_output or "{}"},
                    {
                        "role": "user",
                        "content": (
                            "The previous object failed the frozen schema or exact-span "
                            f"checks ({validation_category}). Return one corrected object only."
                        ),
                    },
                ]
            )
        request_hash = canonical_sha256(
            {
                "source_sha256": source_sha256,
                "model": self.model,
                "endpoint_sha256": _sha256_text(self.base_url),
                "prompt_sha256": self.prompt_sha256,
                "schema_sha256": self.schema_sha256,
                "schema_canonical_sha256": self.schema_canonical_sha256,
                "config_sha256": self.config_sha256,
                "effective_config_sha256": self.effective_config_sha256,
                "attempt": attempt,
                "validation_category": validation_category,
                "prior_response_sha256": (
                    _sha256_text(prior_output)
                    if prior_output is not None
                    else "NOT_APPLICABLE"
                ),
            }
        )
        started = time.perf_counter()
        receipt.update(
            {
                "attempt": attempt,
                "request_sha256": request_hash,
                "prior_response_sha256": (
                    _sha256_text(prior_output)
                    if prior_output is not None
                    else "NOT_APPLICABLE"
                ),
                "response_sha256": "NOT_RECEIVED",
                "provider_request_id_sha256": "NOT_RECEIVED",
                "provider_response_id_sha256": "NOT_RECEIVED",
                "provider_reported_model": "NOT_RECEIVED",
                "http_status": "NOT_EXPOSED_BY_SDK",
                "input_tokens": 0,
                "output_tokens": 0,
                "latency_ms": 0.0,
                "outcome": "NO_RESPONSE",
            }
        )
        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=self.temperature,
                max_tokens=self.max_output_tokens,
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "trip_understanding_draft",
                        "strict": True,
                        "schema": self.schema,
                    },
                },
                extra_body={"enable_thinking": False},
            )
        finally:
            receipt["latency_ms"] = round(
                (time.perf_counter() - started) * 1000,
                3,
            )
        content = response.choices[0].message.content
        if not isinstance(content, str) or not content.strip():
            raise ValueError("EMPTY_STRUCTURED_OUTPUT")
        usage = getattr(response, "usage", None)
        input_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
        provider_request_id = getattr(response, "_request_id", None)
        provider_response_id = getattr(response, "id", None)
        provider_reported_model = getattr(response, "model", None)
        receipt.update(
            {
            "response_sha256": _sha256_text(content),
            "provider_request_id_sha256": (
                _sha256_text(provider_request_id)
                if isinstance(provider_request_id, str) and provider_request_id
                else "NOT_EXPOSED_BY_PROVIDER"
            ),
            "provider_response_id_sha256": (
                _sha256_text(provider_response_id)
                if isinstance(provider_response_id, str) and provider_response_id
                else "NOT_EXPOSED_BY_PROVIDER"
            ),
            "provider_reported_model": (
                provider_reported_model
                if isinstance(provider_reported_model, str) and provider_reported_model
                else "NOT_EXPOSED_BY_PROVIDER"
            ),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "outcome": "RESPONSE_RECEIVED",
            }
        )
        return content

    @staticmethod
    def _proposal_from_draft(
        source_text: str,
        draft: QwenSemanticDraft,
    ) -> tuple[list[ProposedMention], str, int, int, int]:
        destination = draft.destination
        destination_span_relocation_count = 0
        if destination.basis == DestinationBasis.EXPLICIT:
            if source_text[
                destination.evidence_span_start : destination.evidence_span_end
            ] != destination.name:
                destination_offsets = []
                offset = source_text.find(destination.name)
                while offset >= 0:
                    destination_offsets.append(offset)
                    offset = source_text.find(destination.name, offset + 1)
                if len(destination_offsets) != 1:
                    raise ValueError("DESTINATION_SPAN_MISMATCH")
                destination_span_relocation_count = 1

        mentions: list[ProposedMention] = []
        seen_spans: set[tuple[int, int]] = set()
        atomic_span_narrowing_count = 0
        atomic_span_relocation_count = 0
        for index, item in enumerate(draft.mentions, start=1):
            if item.span_end > len(source_text):
                raise ValueError("MENTION_SPAN_OUT_OF_RANGE")
            span_start = item.span_start
            span_end = item.span_end
            raw_text = source_text[span_start:span_end]
            atomic = item.atomic_place_name.strip() if item.atomic_place_name else None
            if atomic:
                lowered = atomic.lower()
                if any(marker in lowered for marker in _FORBIDDEN_ATOMIC_MARKERS):
                    raise ValueError("FORBIDDEN_ATOMIC_PLACE")
                if any(marker in atomic for marker in _SENTENCE_MARKERS):
                    raise ValueError("NON_ATOMIC_PLACE")
                offsets = []
                offset = raw_text.find(atomic)
                while offset >= 0:
                    offsets.append(offset)
                    offset = raw_text.find(atomic, offset + 1)
                if len(offsets) == 1:
                    narrowed_start = span_start + offsets[0]
                else:
                    source_offsets = []
                    source_offset = source_text.find(atomic)
                    while source_offset >= 0:
                        source_offsets.append(source_offset)
                        source_offset = source_text.find(atomic, source_offset + 1)
                    if len(source_offsets) != 1:
                        raise ValueError("ATOMIC_PLACE_SPAN_MISMATCH")
                    narrowed_start = source_offsets[0]
                    atomic_span_relocation_count += 1
                narrowed_end = narrowed_start + len(atomic)
                if (narrowed_start, narrowed_end) != (span_start, span_end):
                    atomic_span_narrowing_count += 1
                span_start, span_end = narrowed_start, narrowed_end
                raw_text = source_text[span_start:span_end]
            span = (span_start, span_end)
            if span in seen_spans:
                raise ValueError("DUPLICATE_MENTION_SPAN")
            seen_spans.add(span)
            mentions.append(
                ProposedMention(
                    mention_id=f"mention-{index}",
                    raw_text=raw_text,
                    span_start=span_start,
                    span_end=span_end,
                    role=item.role,
                    day_index=item.day_index,
                    sequence_index=item.sequence_index,
                    atomic_place_name=atomic,
                    category_hint=item.category_hint,
                    time_hint=item.time_hint,
                )
            )
        return (
            mentions,
            destination.name,
            atomic_span_narrowing_count,
            atomic_span_relocation_count,
            destination_span_relocation_count,
        )

    async def propose(self, source_text: str) -> InferenceProposal:
        calls: list[dict[str, object]] = []
        last_output: str | None = None
        validation_category: str | None = None
        started = time.perf_counter()
        try:
            async with asyncio.timeout(self.deadline_seconds):
                for attempt in ("INITIAL", "SCHEMA_REPAIR"):
                    if attempt == "SCHEMA_REPAIR" and validation_category is None:
                        break
                    receipt: dict[str, object] = {}
                    calls.append(receipt)
                    try:
                        content = await self._call(
                            source_text=source_text,
                            attempt=attempt,
                            receipt=receipt,
                            prior_output=last_output,
                            validation_category=validation_category,
                        )
                        last_output = content
                        draft = QwenSemanticDraft.model_validate_json(content)
                        (
                            mentions,
                            destination_name,
                            atomic_span_narrowing_count,
                            atomic_span_relocation_count,
                            destination_span_relocation_count,
                        ) = self._proposal_from_draft(source_text, draft)
                    except (ValidationError, json.JSONDecodeError, ValueError) as exc:
                        validation_category = _redacted_validation_category(exc)
                        receipt["validation_failure"] = validation_category
                        if attempt == "SCHEMA_REPAIR":
                            raise InferenceProviderUnavailableError(
                                "SCHEMA_REPAIR_EXHAUSTED",
                                provider_binding=self._failure_binding(
                                    calls,
                                    started=started,
                                    category="SCHEMA_REPAIR_EXHAUSTED",
                                ),
                                external_call_count=len(calls),
                            ) from exc
                        continue

                    input_tokens = sum(int(call["input_tokens"]) for call in calls)
                    output_tokens = sum(int(call["output_tokens"]) for call in calls)
                    cost, cost_status = _estimated_cost(
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        input_cny_per_million=self.input_cny_per_million,
                        output_cny_per_million=self.output_cny_per_million,
                    )
                    binding: dict[str, object] = {
                        "provider": "QWEN",
                        "execution_mode": "LIVE",
                        "exact_model_id": self.model,
                        "endpoint_sha256": _sha256_text(self.base_url),
                        "prompt_sha256": self.prompt_sha256,
                        "prompt_artifact_sha256": self.prompt_artifact_sha256,
                        "schema_sha256": self.schema_sha256,
                        "schema_artifact_sha256": self.schema_artifact_sha256,
                        "schema_canonical_sha256": self.schema_canonical_sha256,
                        "config_sha256": self.config_sha256,
                        "config_artifact_sha256": self.config_artifact_sha256,
                        "effective_config_sha256": self.effective_config_sha256,
                        "model_panel_sha256": self.model_panel_sha256,
                        "thinking": False,
                        "temperature": self.temperature,
                        "deadline_ms": round(self.deadline_seconds * 1000),
                        "external_calls": len(calls),
                        "repair_call_count": max(0, len(calls) - 1),
                        "atomic_span_narrowing_count": atomic_span_narrowing_count,
                        "atomic_span_relocation_count": atomic_span_relocation_count,
                        "destination_span_relocation_count": (
                            destination_span_relocation_count
                        ),
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                        "latency_ms": round((time.perf_counter() - started) * 1000, 3),
                        "estimated_cost_cny": cost,
                        "estimated_cost_status": cost_status,
                        "calls": calls,
                        "raw_request_or_response_retained": False,
                    }
                    return InferenceProposal(
                        source_hash=_sha256_text(source_text),
                        destination_name=destination_name,
                        destination_basis=draft.destination.basis,
                        mentions=mentions,
                        binding=binding,
                    )
        except asyncio.TimeoutError as exc:
            raise InferenceProviderUnavailableError(
                "DEADLINE_EXCEEDED",
                provider_binding=self._failure_binding(
                    calls,
                    started=started,
                    category="DEADLINE_EXCEEDED",
                ),
                external_call_count=len(calls) or 1,
            ) from exc
        except InferenceProviderUnavailableError:
            raise
        except Exception as exc:
            raise InferenceProviderUnavailableError(
                "PROVIDER_UNAVAILABLE",
                provider_binding=self._failure_binding(
                    calls,
                    started=started,
                    category="PROVIDER_UNAVAILABLE",
                ),
                external_call_count=len(calls) or 1,
            ) from exc
        raise InferenceProviderUnavailableError(
            "SCHEMA_INVALID",
            provider_binding=self._failure_binding(
                calls,
                started=started,
                category="SCHEMA_INVALID",
            ),
            external_call_count=len(calls),
        )

    def _failure_binding(
        self,
        calls: list[dict[str, object]],
        *,
        started: float,
        category: str,
    ) -> dict[str, object]:
        input_tokens = sum(int(call["input_tokens"]) for call in calls)
        output_tokens = sum(int(call["output_tokens"]) for call in calls)
        cost, cost_status = _estimated_cost(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            input_cny_per_million=self.input_cny_per_million,
            output_cny_per_million=self.output_cny_per_million,
        )
        return {
            "provider": "QWEN",
            "execution_mode": "LIVE",
            "exact_model_id": self.model,
            "endpoint_sha256": _sha256_text(self.base_url),
            "prompt_sha256": self.prompt_sha256,
            "prompt_artifact_sha256": self.prompt_artifact_sha256,
            "schema_sha256": self.schema_sha256,
            "schema_artifact_sha256": self.schema_artifact_sha256,
            "schema_canonical_sha256": self.schema_canonical_sha256,
            "config_sha256": self.config_sha256,
            "config_artifact_sha256": self.config_artifact_sha256,
            "effective_config_sha256": self.effective_config_sha256,
            "model_panel_sha256": self.model_panel_sha256,
            "failure_category": category,
            "thinking": False,
            "temperature": self.temperature,
            "deadline_ms": round(self.deadline_seconds * 1000),
            "external_calls": len(calls),
            "repair_call_count": max(0, len(calls) - 1),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "latency_ms": round((time.perf_counter() - started) * 1000, 3),
            "estimated_cost_cny": cost,
            "estimated_cost_status": cost_status,
            "calls": calls,
            "raw_request_or_response_retained": False,
        }
