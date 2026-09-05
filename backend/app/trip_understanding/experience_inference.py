"""Live semantic extraction for the experience app.

The model owns meaning and order. This adapter validates source anchors and
structure; it never manufactures POIs or rewrites meaning with a local parser.
The legacy frozen adapter remains available for historical experiments.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any, Literal

from openai import APIError, AsyncOpenAI
from pydantic import Field, ValidationError

from app.trip_understanding.errors import InferenceProviderUnavailableError
from app.trip_understanding.models import (
    ActivityRole, ActivityTiming, DestinationBasis, InferenceProposal,
    ProposedMention, StrictModel,
)
from app.trip_understanding.pipeline import atomic_place_rejection_reason


PROMPT_PATH = Path(__file__).with_name("experience_inference_prompt.md")
SEMANTIC_POLICY = "MODEL_MEANING_SOURCE_VALIDATED_V1"


class SemanticActivity(ActivityTiming):
    source_quote: str = Field(min_length=1, max_length=1000)
    occurrence: int = Field(default=1, ge=1, le=160)
    place_name: str | None = Field(default=None, max_length=40)
    role: ActivityRole
    day_index: int | None = Field(default=None, ge=1, le=14)
    category: Literal["景点", "餐饮", "住宿", "交通节点", "地点"] = "地点"
    time_evidence: str | None = Field(default=None, max_length=500)


class SemanticDraft(StrictModel):
    destination: str = Field(min_length=1, max_length=40)
    day_labels: list[str | None] = Field(default_factory=list, max_length=14)
    activities: list[SemanticActivity] = Field(max_length=160)
    unprocessed_quotes: list[str] = Field(default_factory=list, max_length=80)


def _source_occurrence(source: str, quote: str, occurrence: int) -> int:
    """Locate an exact model-selected source quote without guessing its meaning."""
    start = -1
    for _ in range(occurrence):
        start = source.find(quote, start + 1)
        if start < 0:
            raise ValueError("SOURCE_QUOTE_NOT_FOUND")
    return start


def proposal_from_draft(source: str, draft: SemanticDraft) -> InferenceProposal:
    mentions: list[ProposedMention] = []
    seen: set[tuple[int, int, ActivityRole, int | None]] = set()
    sequences: dict[int, int] = {}
    unprocessed = len(draft.unprocessed_quotes)
    for quote in draft.unprocessed_quotes:
        _source_occurrence(source, quote, 1)
    for item in draft.activities:
        quote_start = _source_occurrence(source, item.source_quote, item.occurrence)
        place = item.place_name.strip() if item.place_name else None
        start, end = quote_start, quote_start + len(item.source_quote)
        if place is not None:
            relative = item.source_quote.find(place)
            if relative < 0:
                raise ValueError("PLACE_NOT_IN_SOURCE_QUOTE")
            start = quote_start + relative
            end = start + len(place)
            if atomic_place_rejection_reason(place) is not None:
                # Keep the intended arrangement pending without presenting a
                # description/URL as a real place or sending it to POI search.
                place = None
                unprocessed += 1
        day = item.day_index
        if item.role == ActivityRole.PLANNED and day is None:
            day = 1
            unprocessed += 1
        signature = (start, end, item.role, day)
        if signature in seen:
            continue
        seen.add(signature)
        timing = item.model_dump(include=set(ActivityTiming.model_fields))
        has_timing = any(timing.get(key) is not None for key in (
            "start_time", "end_time", "visit_duration_minutes",
        ))
        if has_timing:
            if not item.time_evidence or item.time_evidence not in source:
                raise ValueError("TIME_EVIDENCE_NOT_IN_SOURCE")
            timing["timing_source"] = "TEXT"
        else:
            timing["timing_source"] = "UNSPECIFIED"
        if timing.get("locked") or timing.get("fixed_commitment"):
            if not item.time_evidence or item.time_evidence not in source:
                raise ValueError("COMMITMENT_EVIDENCE_NOT_IN_SOURCE")
        group = day or 0
        sequence = sequences.get(group, 0)
        sequences[group] = sequence + 1
        start_time, end_time = timing.get("start_time"), timing.get("end_time")
        hint = f"{start_time}–{end_time}" if start_time and end_time else start_time
        mentions.append(ProposedMention(
            mention_id=f"activity-{len(mentions) + 1}",
            raw_text=source[start:end], span_start=start, span_end=end,
            role=item.role, day_index=day, sequence_index=sequence,
            atomic_place_name=place, category_hint=item.category,
            time_hint=hint, **timing,
        ))
    labels: dict[int, str] = {}
    for index, label in enumerate(draft.day_labels, 1):
        if label and label in source and re.fullmatch(r"[\d年月日号./\-一二三四五六七八九十星期周\s]+", label):
            labels[index] = label.strip()
        elif label:
            unprocessed += 1
    return InferenceProposal(
        source_hash=hashlib.sha256(source.encode()).hexdigest(),
        destination_name=draft.destination,
        destination_basis=(DestinationBasis.EXPLICIT if draft.destination in source
                           else DestinationBasis.SOFT_ASSUMPTION),
        day_labels=labels, unprocessed_count=unprocessed,
        mentions=mentions, binding={"semantic_policy": SEMANTIC_POLICY},
    )


class ExperienceQwenProvider:
    def __init__(
        self, *, api_key: str, base_url: str, model: str,
        deadline_seconds: float = 30, max_output_tokens: int = 4096,
        input_cny_per_million: float | None = None,
        output_cny_per_million: float | None = None,
        client: Any | None = None,
    ) -> None:
        if not api_key or not model or not base_url.startswith("https://"):
            raise ValueError("Live inference requires configured HTTPS credentials and model")
        if deadline_seconds <= 0 or max_output_tokens < 256:
            raise ValueError("Invalid inference budget")
        self.model = model
        self.deadline_seconds = deadline_seconds
        self.max_output_tokens = max_output_tokens
        self.rates = (input_cny_per_million, output_cny_per_million)
        self.prompt = PROMPT_PATH.read_text(encoding="utf-8")
        self.schema = SemanticDraft.model_json_schema()
        self._owned = client is None
        self.client = client or AsyncOpenAI(
            api_key=api_key, base_url=base_url, timeout=deadline_seconds, max_retries=0,
        )
        self._slots = asyncio.Semaphore(1)

    async def aclose(self) -> None:
        if self._owned:
            await self.client.close()
            self._owned = False

    async def propose(self, source_text: str) -> InferenceProposal:
        # The deadline measures a Provider run, excluding queue backpressure.
        async with self._slots:
            return await self._propose(source_text)

    async def _propose(self, source_text: str) -> InferenceProposal:
        started = time.perf_counter()
        calls: list[dict[str, object]] = []
        messages = [
            {"role": "system", "content": self.prompt + "\nJSON Schema:\n" + json.dumps(self.schema, ensure_ascii=False)},
            {"role": "user", "content": source_text},
        ]
        failure = "INVALID_STRUCTURED_OUTPUT"
        proposal: InferenceProposal | None = None
        try:
            async with asyncio.timeout(self.deadline_seconds):
                for attempt in range(2):
                    call: dict[str, object] = {"attempt": attempt + 1, "input_tokens": None, "output_tokens": None, "outcome": "UNKNOWN"}
                    calls.append(call)
                    call_started = time.perf_counter()
                    try:
                        response = await self.client.chat.completions.create(
                            model=self.model, messages=messages, temperature=0.1,
                            max_tokens=self.max_output_tokens,
                            response_format={"type": "json_object"},
                            extra_body={"enable_thinking": False},
                        )
                    finally:
                        call["latency_ms"] = round((time.perf_counter() - call_started) * 1000, 2)
                    usage = getattr(response, "usage", None)
                    call["input_tokens"] = getattr(usage, "prompt_tokens", None)
                    call["output_tokens"] = getattr(usage, "completion_tokens", None)
                    call["reported_model"] = getattr(response, "model", None)
                    content = response.choices[0].message.content or ""
                    call["response_sha256"] = hashlib.sha256(content.encode()).hexdigest()
                    try:
                        if getattr(response.choices[0], "finish_reason", None) == "length":
                            raise ValueError("OUTPUT_TRUNCATED")
                        draft = SemanticDraft.model_validate_json(content)
                        proposal = proposal_from_draft(source_text, draft)
                    except (ValueError, ValidationError) as exc:
                        failure = "INVALID_STRUCTURED_OUTPUT" if isinstance(exc, ValidationError) else str(exc)
                        call["outcome"] = failure
                        if attempt == 0:
                            messages.extend([
                                {"role": "assistant", "content": content},
                                {"role": "user", "content": "重新检查 JSON 结构、原文逐字引用、日期和时间。错误类别：" + failure + "。只返回修正后的完整 JSON；不要补造原文信息。"},
                            ])
                        continue
                    call["outcome"] = "SUCCESS"
                    break
        except TimeoutError:
            failure = "DEADLINE_EXCEEDED"
        except APIError:
            failure = "PROVIDER_UNAVAILABLE"
        known_usage = all(isinstance(c.get("input_tokens"), int) and isinstance(c.get("output_tokens"), int) for c in calls)
        input_tokens = sum(int(c["input_tokens"]) for c in calls) if known_usage else None
        output_tokens = sum(int(c["output_tokens"]) for c in calls) if known_usage else None
        cost = None
        if known_usage and all(rate is not None for rate in self.rates):
            cost = round((input_tokens * self.rates[0] + output_tokens * self.rates[1]) / 1_000_000, 8)
        binding = {
            "provider": "QWEN", "model": self.model, "semantic_policy": SEMANTIC_POLICY,
            "prompt_sha256": hashlib.sha256(self.prompt.encode()).hexdigest(),
            "schema_sha256": hashlib.sha256(json.dumps(self.schema, sort_keys=True).encode()).hexdigest(),
            "deadline_ms": round(self.deadline_seconds * 1000), "max_output_tokens": self.max_output_tokens,
            "external_calls": len(calls), "repair_call_count": max(0, len(calls) - 1),
            "fallback_used": False, "latency_ms": round((time.perf_counter() - started) * 1000, 2),
            "input_tokens": input_tokens, "output_tokens": output_tokens,
            "estimated_cost_cny": cost, "calls": calls,
            "outcome": "SUCCESS" if proposal is not None else failure,
        }
        if proposal is None:
            raise InferenceProviderUnavailableError(
                failure, provider_binding=binding, external_call_count=len(calls),
            ) from None
        return proposal.model_copy(update={"binding": binding})
