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
    source_quote: str = Field(
        min_length=1, max_length=1000,
        description="能定位这一项的最短原文片段；有地点时优先只引用地点名，不复制整段说明。",
    )
    occurrence: int = Field(default=1, ge=1, le=160)
    place_name: str | None = Field(default=None, max_length=40)
    role: ActivityRole
    day_index: int | None = Field(default=None, ge=1, le=14)
    category: Literal["景点", "餐饮", "住宿", "交通节点", "地点"] = "地点"
    time_evidence: str | None = Field(default=None, max_length=500)


class SemanticDraft(StrictModel):
    destination: str = Field(min_length=1, max_length=40)
    day_labels: list[str | None] = Field(default_factory=list, max_length=14)
    activities: list[SemanticActivity] = Field(
        max_length=160,
        description="按执行顺序逐地点列出；同句并列的多个独立地点分别成项，二选一的两个地点都保留为OPTIONAL。",
    )
    unprocessed_quotes: list[str] = Field(default_factory=list, max_length=80)


class SourceAnchorValidationError(ValueError):
    """Only field locations and categories; source text never enters failure logs."""

    def __init__(self, issues: list[dict[str, object]]) -> None:
        self.issues = issues
        self.category = str(issues[0]["category"])
        super().__init__(self.category)


def _markdown_visible(source: str) -> tuple[str, list[int]]:
    """Remove paired inline decoration with a reversible character index.

    No word, punctuation, whitespace, link destination or Unicode character is
    corrected. A match can differ only by balanced Markdown delimiters. Place
    names must still occur literally inside the resulting original source span.
    """
    hidden: set[int] = set()
    # Strong/emphasis and inline code are presentation, not itinerary meaning.
    # Longest delimiters first handles ***text*** without consuming nested runs.
    for delimiter in ("***", "___", "**", "__", "`", "*", "_"):
        escaped = re.escape(delimiter)
        boundary = r"\\\w" if delimiter[0] == "_" else "\\" + re.escape(delimiter[0])
        pattern = re.compile(
            rf"(?<![{boundary}])(?P<open>{escaped})(?!{re.escape(delimiter[0])})(?=\S)"
            rf"(?P<body>[^\r\n]+?)(?<=\S)(?<!\\)(?P<close>{escaped})(?!{re.escape(delimiter[0])})"
        )
        for match in pattern.finditer(source):
            opened = range(*match.span("open"))
            closed = range(*match.span("close"))
            if any(index in hidden for index in (*opened, *closed)):
                continue
            hidden.update(opened)
            hidden.update(closed)
    indices = [index for index in range(len(source)) if index not in hidden]
    return "".join(source[index] for index in indices), indices


class SourceAnchorIndex:
    def __init__(self, source: str) -> None:
        self.source = source
        self.visible, self.indices = _markdown_visible(source)

    def locate(self, quote: str, occurrence: int = 1) -> tuple[int, int]:
        visible_quote, _indices = _markdown_visible(quote)
        if not visible_quote:
            raise ValueError("SOURCE_QUOTE_NOT_FOUND")
        start = -1
        for _ in range(occurrence):
            start = self.visible.find(visible_quote, start + 1)
            if start < 0:
                raise ValueError("SOURCE_QUOTE_NOT_FOUND")
        return self.indices[start], self.indices[start + len(visible_quote) - 1] + 1


def _source_occurrence(source: str, quote: str, occurrence: int) -> int:
    return SourceAnchorIndex(source).locate(quote, occurrence)[0]


def _validation_issues(exc: ValueError) -> list[dict[str, object]]:
    if isinstance(exc, SourceAnchorValidationError):
        return exc.issues[:20]
    if isinstance(exc, ValidationError):
        known_fields = set(SemanticDraft.model_fields) | set(SemanticActivity.model_fields)
        issues = []
        for error in exc.errors(include_input=False, include_context=False, include_url=False)[:20]:
            field = ""
            for segment in error["loc"]:
                if isinstance(segment, int):
                    field += f"[{segment}]"
                else:
                    name = segment if segment in known_fields else "unknown_field"
                    field += ("." if field else "") + name
            issues.append({"field": field or "document", "category": str(error["type"])})
        return issues
    return [{"field": "document", "category": "OUTPUT_TRUNCATED"}]


def _explicit_markdown_place_groups(source: str) -> tuple[tuple[str, ...], ...]:
    """Find short, explicitly grouped Markdown labels without parsing prose."""

    groups: list[tuple[str, ...]] = []
    for match in re.finditer(r"(?:\*\*|__)(?P<body>[^\r\n]{3,80}?)(?:\*\*|__)", source):
        body = match.group("body").strip()
        lead_in = source[max(0, match.start() - 32):match.start()]
        if re.search(r"(?:不想|可以|可选|备选|推荐|例如|比如|隔壁)[^。！？；\n]{0,24}$", lead_in):
            continue
        if not re.search(r"\+|、|，|,|/|／", body) or re.search(r"[（）()：:；;。！？]", body):
            continue
        parts = tuple(part.strip() for part in re.split(r"\s*(?:\+|、|，|,|/|／)\s*", body))
        if 2 <= len(parts) <= 8 and all(
            part and len(part) <= 40 and atomic_place_rejection_reason(part) is None
            for part in parts
        ):
            groups.append(parts)
    return tuple(groups)


def proposal_from_draft(source: str, draft: SemanticDraft) -> InferenceProposal:
    anchors = SourceAnchorIndex(source)
    issues: list[dict[str, object]] = []
    located: list[tuple[int, int]] = []
    proposed_atomic = {
        item.place_name.strip()
        for item in draft.activities
        if item.place_name and item.place_name.strip()
        and not re.search(r"\+|、|，|,|/|／", item.place_name)
    }
    for group_index, group in enumerate(_explicit_markdown_place_groups(source)):
        if not set(group).issubset(proposed_atomic):
            issues.append({
                "field": f"activities.parallel_group[{group_index}]",
                "category": "MISSING_EXPLICIT_PARALLEL_PLACE",
            })
    for index, quote in enumerate(draft.unprocessed_quotes):
        try:
            anchors.locate(quote)
        except ValueError:
            issues.append({"field": f"unprocessed_quotes[{index}]", "category": "SOURCE_QUOTE_NOT_FOUND"})
    for index, item in enumerate(draft.activities):
        try:
            start, end = anchors.locate(item.source_quote, item.occurrence)
            located.append((start, end))
        except ValueError:
            issues.append({"field": f"activities[{index}].source_quote", "category": "SOURCE_QUOTE_NOT_FOUND"})
            located.append((0, 0))
        else:
            place = item.place_name.strip() if item.place_name else None
            if place and place not in source[start:end]:
                issues.append({"field": f"activities[{index}].place_name", "category": "PLACE_NOT_IN_SOURCE_QUOTE"})
            # A planned sightseeing/location item containing an explicit list
            # must be returned one atomic place per activity. Rejecting the
            # bundled draft asks the model's bounded repair pass to preserve
            # every source-grounded place; the adapter still never guesses or
            # manufactures a POI from prose.
            visible_quote, _ = _markdown_visible(item.source_quote)
            atomic_siblings = {
                (sibling.place_name or "").strip()
                for sibling in draft.activities
                if sibling.source_quote == item.source_quote
                and sibling.occurrence == item.occurrence
                and sibling.day_index == item.day_index
                and sibling.role == item.role
                and (sibling.place_name or "").strip()
                and not re.search(r"\+|、|，|,|/|／", sibling.place_name or "")
            }
            if (
                item.role in {ActivityRole.PLANNED, ActivityRole.OPTIONAL}
                and item.category in {"景点", "地点", "交通节点", "住宿"}
                and re.search(r"\S\s*(?:\+|、|，|,|/|／)\s*\S", visible_quote)
                and (not place or re.search(r"\+|、|，|,|/|／", place))
                and len(atomic_siblings) < 2
            ):
                issues.append({
                    "field": f"activities[{index}].place_name",
                    "category": "NON_ATOMIC_PLACE_LIST",
                })
        has_timing = any(getattr(item, key) is not None for key in (
            "start_time", "end_time", "visit_duration_minutes",
        ))
        if has_timing or item.locked or item.fixed_commitment:
            try:
                if not item.time_evidence:
                    raise ValueError
                anchors.locate(item.time_evidence)
            except ValueError:
                issues.append({"field": f"activities[{index}].time_evidence", "category": (
                    "TIME_EVIDENCE_NOT_IN_SOURCE" if has_timing else "COMMITMENT_EVIDENCE_NOT_IN_SOURCE"
                )})
    if issues:
        raise SourceAnchorValidationError(issues)
    mentions: list[ProposedMention] = []
    seen: set[tuple[int, int, ActivityRole, int | None]] = set()
    sequences: dict[int, int] = {}
    unprocessed = len(draft.unprocessed_quotes)
    for item, (start, end) in zip(draft.activities, located, strict=True):
        place = item.place_name.strip() if item.place_name else None
        if place is not None:
            relative = source[start:end].index(place)
            start += relative
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
            timing["timing_source"] = "TEXT"
        else:
            timing["timing_source"] = "UNSPECIFIED"
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
                        call["validation_errors"] = _validation_issues(exc)
                        if attempt == 0:
                            messages.extend([
                                {"role": "assistant", "content": content},
                                {"role": "user", "content": (
                                    "只修复以下字段，保留其他已正确整理的活动、顺序和角色，不要为绕过错误删除活动。"
                                    "source_quote 优先缩短为原文中该地点的逐字名称；occurrence 按去掉 Markdown 装饰后的可见片段计数。"
                                    "place_name 仍必须逐字出现在对应原文范围内，不得改写、补全或模糊猜测。"
                                    "NON_ATOMIC_PLACE_LIST 表示把多个地点压成了一项：请按原文顺序拆成多个活动，"
                                    "每项 source_quote 和 place_name 都使用该地点的逐字名称；二选一分别标 OPTIONAL。"
                                    "MISSING_EXPLICIT_PARALLEL_PLACE 表示 Markdown 强调的并列地点仍有遗漏；"
                                    "重新逐项核对所有加粗并列组，每个地点必须各有一项，不能只留第一项。"
                                    "时间没有原文依据就清除时间字段并把真实原文片段放入 unprocessed_quotes。"
                                    "字段错误（从0开始）：" + json.dumps(call["validation_errors"], ensure_ascii=False)
                                    + "。只返回修正后的完整 JSON，不要补造原文信息。"
                                )},
                            ])
                        continue
                    call["outcome"] = "SUCCESS"
                    break
        except TimeoutError:
            failure = "DEADLINE_EXCEEDED"
            if calls:
                calls[-1]["outcome"] = failure
        except APIError:
            failure = "PROVIDER_UNAVAILABLE"
            if calls:
                calls[-1]["outcome"] = failure
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
