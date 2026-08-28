from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from openai import AsyncOpenAI
from pydantic import Field, ValidationError

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
_NON_ATOMIC_SOURCE_MARKERS = (
    "上午",
    "下午",
    "随后",
    "前往",
    "游览",
    "安排",
    "确定",
    "如果",
    "只是",
    "网友",
    "提到",
    "参考",
    "备选",
    "路过",
    "经过",
    "换乘",
    "放在",
    "不去",
    "排除",
    "很有名",
    "去",
    "到",
    "与",
)
_NON_ATOMIC_SOURCE_CHARACTERS = set("。！？；\n，,:：;、")
_URL_TOKEN_RE = re.compile(r"https?://[^\s，。；！？]+", re.IGNORECASE)
_DAY_NUMBER = {
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}
_DAY_HEADING_RE = re.compile(
    r"(?:第\s*(?P<zh>[一二三四五六七八九十])\s*天|(?:Day|D)\s*(?P<num>1[0-4]|[1-9]))",
    re.IGNORECASE,
)


def _verbatim_offsets_outside_urls(source_text: str, value: str) -> list[int]:
    url_spans = [match.span() for match in _URL_TOKEN_RE.finditer(source_text)]
    offsets: list[int] = []
    offset = source_text.find(value)
    while offset >= 0:
        end = offset + len(value)
        if not any(offset < url_end and end > url_start for url_start, url_end in url_spans):
            offsets.append(offset)
        offset = source_text.find(value, offset + 1)
    return offsets


def _day_index_at(source_text: str, position: int) -> int:
    day_index = 1
    for match in _DAY_HEADING_RE.finditer(source_text):
        if match.start() > position:
            break
        raw = match.group("zh") or match.group("num")
        day_index = int(raw) if raw.isdigit() else _DAY_NUMBER[raw]
    return day_index


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
        "ATOMIC_PLACE_NOT_VERBATIM",
        "ATOMIC_PLACE_AMBIGUOUS_SPAN",
        "FORBIDDEN_ATOMIC_PLACE",
        "NON_ATOMIC_PLACE",
        "EMPTY_STRUCTURED_OUTPUT",
    }:
        return message
    return "VALUE_ERROR"


def _safe_atomic_source_span(
    source_text: str,
    span_start: int,
    span_end: int,
) -> tuple[int, int, str] | None:
    if not 0 <= span_start < span_end <= len(source_text):
        return None
    selected = source_text[span_start:span_end]
    value = selected.strip()
    if not value or len(value) > 40:
        return None
    start = span_start + len(selected) - len(selected.lstrip())
    end = start + len(value)
    if any(
        match.start() < end and match.end() > start
        for match in _URL_TOKEN_RE.finditer(source_text)
    ):
        return None
    lowered = value.casefold()
    if any(marker in lowered for marker in _FORBIDDEN_ATOMIC_MARKERS):
        return None
    if any(marker in value for marker in _NON_ATOMIC_SOURCE_CHARACTERS):
        return None
    if any(marker in value for marker in _NON_ATOMIC_SOURCE_MARKERS):
        return None
    return start, end, value


class QwenExplicitDestinationDraft(StrictModel):
    name: str = Field(min_length=1, max_length=40)
    basis: Literal[DestinationBasis.EXPLICIT]
    evidence_span_start: int = Field(ge=0)
    evidence_span_end: int = Field(gt=0)

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

class QwenSemanticDraft(StrictModel):
    destination: QwenDestinationDraft
    mentions: list[QwenMentionDraft] = Field(max_length=160)


def qwen_semantic_schema() -> dict[str, Any]:
    schema = QwenSemanticDraft.model_json_schema()

    def preserve_literal_enum(value: object) -> None:
        """Keep the frozen wire schema stable across Pydantic patch releases."""

        if isinstance(value, dict):
            if "const" in value and "enum" not in value:
                value["enum"] = [value["const"]]
            for child in value.values():
                preserve_literal_enum(child)
        elif isinstance(value, list):
            for child in value:
                preserve_literal_enum(child)

    preserve_literal_enum(schema)
    return schema


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
        started_at = datetime.now(UTC)
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
                "started_at": started_at.isoformat().replace("+00:00", "Z"),
                "completed_at": "NOT_COMPLETED",
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
            receipt["completed_at"] = datetime.now(UTC).isoformat().replace(
                "+00:00", "Z"
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
    ) -> tuple[list[ProposedMention], str, int, int, int, int, int, int]:
        destination = draft.destination
        destination_span_relocation_count = 0
        if destination.basis == DestinationBasis.EXPLICIT:
            destination_span = (
                destination.evidence_span_start,
                destination.evidence_span_end,
            )
            destination_offsets = _verbatim_offsets_outside_urls(
                source_text,
                destination.name,
            )
            if not destination_offsets:
                raise ValueError("DESTINATION_SPAN_MISMATCH")
            if (
                source_text[slice(*destination_span)] != destination.name
                or destination_span[0] not in destination_offsets
            ):
                destination_span_relocation_count = 1

        mentions: list[ProposedMention] = []
        seen_spans: set[tuple[int, int]] = set()
        atomic_span_narrowing_count = 0
        atomic_span_relocation_count = 0
        atomic_span_disambiguation_count = 0
        atomic_name_source_recovery_count = 0
        planned_day_fill_count = 0
        for index, item in enumerate(draft.mentions, start=1):
            span_start = item.span_start
            span_end = item.span_end
            atomic = item.atomic_place_name.strip() if item.atomic_place_name else None
            if atomic:
                lowered = atomic.lower()
                if any(marker in lowered for marker in _FORBIDDEN_ATOMIC_MARKERS):
                    raise ValueError("FORBIDDEN_ATOMIC_PLACE")
                if any(marker in atomic for marker in _SENTENCE_MARKERS):
                    raise ValueError("NON_ATOMIC_PLACE")
                source_offsets = [
                    offset
                    for offset in _verbatim_offsets_outside_urls(source_text, atomic)
                    if (offset, offset + len(atomic)) not in seen_spans
                ]
                if not source_offsets:
                    recovered = _safe_atomic_source_span(
                        source_text,
                        span_start,
                        span_end,
                    )
                    if recovered is None:
                        raise ValueError("ATOMIC_PLACE_NOT_VERBATIM")
                    span_start, span_end, atomic = recovered
                    source_offsets = [span_start]
                    atomic_name_source_recovery_count += 1
                offsets_in_span = [
                    offset
                    for offset in source_offsets
                    if 0 <= span_start < span_end <= len(source_text)
                    and span_start <= offset
                    and offset + len(atomic) <= span_end
                ]
                if len(offsets_in_span) == 1:
                    narrowed_start = offsets_in_span[0]
                else:
                    if len(source_offsets) == 1:
                        narrowed_start = source_offsets[0]
                    else:
                        proposed_midpoint = (span_start + span_end) / 2
                        ranked_offsets = sorted(
                            source_offsets,
                            key=lambda offset: (
                                abs((offset + len(atomic) / 2) - proposed_midpoint),
                                offset,
                            ),
                        )
                        best_distance = abs(
                            (ranked_offsets[0] + len(atomic) / 2)
                            - proposed_midpoint
                        )
                        equally_near = [
                            offset
                            for offset in ranked_offsets
                            if abs(
                                abs((offset + len(atomic) / 2) - proposed_midpoint)
                                - best_distance
                            )
                            < 1e-9
                        ]
                        if len(equally_near) != 1:
                            raise ValueError("ATOMIC_PLACE_AMBIGUOUS_SPAN")
                        narrowed_start = equally_near[0]
                        atomic_span_disambiguation_count += 1
                    atomic_span_relocation_count += 1
                narrowed_end = narrowed_start + len(atomic)
                if (narrowed_start, narrowed_end) != (span_start, span_end):
                    atomic_span_narrowing_count += 1
                span_start, span_end = narrowed_start, narrowed_end
            elif not 0 <= span_start < span_end <= len(source_text):
                raise ValueError("MENTION_SPAN_OUT_OF_RANGE")
            raw_text = source_text[span_start:span_end]
            day_index = item.day_index
            if item.role == ActivityRole.PLANNED and day_index is None:
                day_index = _day_index_at(source_text, span_start)
                planned_day_fill_count += 1
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
                    day_index=day_index,
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
            atomic_span_disambiguation_count,
            destination_span_relocation_count,
            planned_day_fill_count,
            atomic_name_source_recovery_count,
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
                            atomic_span_disambiguation_count,
                            destination_span_relocation_count,
                            planned_day_fill_count,
                            atomic_name_source_recovery_count,
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
                        "atomic_span_disambiguation_count": (
                            atomic_span_disambiguation_count
                        ),
                        "destination_span_relocation_count": (
                            destination_span_relocation_count
                        ),
                        "planned_day_fill_count": planned_day_fill_count,
                        "atomic_name_source_recovery_count": (
                            atomic_name_source_recovery_count
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
        usage_complete = all(
            call.get("outcome") == "RESPONSE_RECEIVED" for call in calls
        )
        if usage_complete:
            cost, cost_status = _estimated_cost(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                input_cny_per_million=self.input_cny_per_million,
                output_cny_per_million=self.output_cny_per_million,
            )
        else:
            cost = None
            cost_status = "NOT_EXPOSED_BY_PROVIDER_FOR_INCOMPLETE_CALL"
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
