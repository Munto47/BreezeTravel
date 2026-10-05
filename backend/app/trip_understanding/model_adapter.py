"""Transport boundary for itinerary semantics; no place or persistence rules."""
from __future__ import annotations

import json
import asyncio
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Literal
from urllib.parse import urlsplit
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ExecutionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    adapter_version: Literal[1] = 1
    provider: Literal["KIMI_CODE", "QWEN"]
    base_url: str
    model: str
    credential_ref: Literal["kimi_for_code", "qwen_api_key", "trip_semantic_api_key"]
    reasoning_effort: Literal["none", "low", "high", "max"] = "high"
    output_mode: Literal["json_schema", "json_object"] = "json_schema"
    deadline_seconds: float = Field(default=180, gt=0)
    max_output_tokens: int = Field(default=8192, ge=256)
    max_calls: int = Field(default=31, ge=1, le=31)
    total_seconds: int = Field(default=600, ge=1, le=600)
    processing_mode: Literal["legacy", "short_stream"] = "legacy"
    stream_protocol_version: Literal[1] = 1
    example_preprocessing: dict | None = None

    @model_validator(mode="after")
    def validate_endpoint(self):
        url = urlsplit(self.base_url)
        if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError("semantic endpoint must be HTTPS without embedded credentials")
        if self.model in {"k3", "k3-256k"} and (self.provider != "KIMI_CODE" or self.reasoning_effort == "none"):
            raise ValueError("K3 requires an explicit Kimi provider and enabled reasoning")
        if self.provider == "QWEN" and self.reasoning_effort != "none":
            raise ValueError("the legacy Qwen adapter supports only explicit non-reasoning requests")
        return self


def execution_config(settings, *, legacy=False, source_text=None, example_reference=None):
    if legacy:
        value = getattr(settings, "trip_semantic_legacy_config", "")
        if not value:
            raise ValueError("legacy semantic execution configuration is missing")
        return ExecutionConfig.model_validate_json(value)
    config = ExecutionConfig(
        provider=settings.trip_semantic_provider, base_url=settings.trip_semantic_base_url,
        model=settings.trip_semantic_model, credential_ref=settings.trip_semantic_credential_ref,
        reasoning_effort=settings.trip_semantic_reasoning_effort, output_mode=settings.trip_semantic_output_mode,
        deadline_seconds=settings.trip_semantic_deadline_seconds, max_output_tokens=settings.trip_semantic_max_output_tokens,
        max_calls=settings.trip_semantic_max_calls, total_seconds=settings.trip_semantic_total_seconds,
    )
    if (getattr(settings, "trip_short_stream_enabled", False) and source_text is not None
            and 0 < len(source_text) <= 500):
        config = config.model_copy(update={"processing_mode": "short_stream", "max_calls": min(2, config.max_calls),
            "reasoning_effort": getattr(settings, "trip_short_stream_reasoning_effort", "low")
                if config.provider == "KIMI_CODE" else "none"})
    if (getattr(settings, "trip_example_preprocessing_enabled", False)
            and config.processing_mode == "short_stream"):
        from app.trip_understanding.example_preprocessing import select_example
        try:
            selection = select_example(source_text, example_reference)
        except (ValueError, KeyError, OSError):
            selection = None
        config = config.model_copy(update={"example_preprocessing": selection})
    return config


_usage_sink = ContextVar("semantic_usage_sink", default=None)


@contextmanager
def record_model_calls(sink):
    token = _usage_sink.set(sink)
    try:
        yield
    finally:
        _usage_sink.reset(token)


class SemanticModelAdapter:
    def __init__(self, config: ExecutionConfig):
        self.config = config
        self.calls: list[dict] = []

    def request_options(self, *, messages, response_format, max_tokens):
        config = self.config
        options = {"model": config.model, "messages": list(messages),
                   "max_tokens": min(max_tokens, config.max_output_tokens), "response_format": response_format}
        if config.output_mode == "json_object":
            options["response_format"] = {"type": "json_object"}
            if response_format.get("type") == "json_schema":
                schema = response_format["json_schema"]["schema"]
                options["messages"] = [{"role": "system", "content": "Return JSON matching this schema:\n" + json.dumps(schema, ensure_ascii=False)}, *messages]
        if config.provider == "KIMI_CODE":
            if config.reasoning_effort == "none":
                options.update(temperature=0.6, extra_body={"thinking": {"type": "disabled"}})
            else:
                options["reasoning_effort"] = config.reasoning_effort
        else:
            options.update(temperature=0, extra_body={"enable_thinking": False})
        return options

    async def complete(self, client, *, messages, response_format, max_tokens):
        options = self.request_options(messages=messages, response_format=response_format, max_tokens=max_tokens)
        record = {"call_id": uuid4().hex, "provider": self.config.provider, "requested_model": options["model"],
                  "reasoning_effort": options.get("reasoning_effort", "none"),
                  "output_mode": options["response_format"]["type"], "max_output_tokens": options["max_tokens"],
                  "temperature": options.get("temperature"), "extra_body": options.get("extra_body"),
                  "status": "DISPATCHING", "input_tokens": None, "output_tokens": None, "reasoning_tokens": None,
                  "reported_model": None, "finish_reason": None}
        sink = _usage_sink.get()
        if sink:
            await sink(dict(record))
        self.calls.append(record)
        started = time.perf_counter()
        try:
            from app.trip_understanding.inference_allowance import remaining_call_seconds
            remaining = remaining_call_seconds()
            timeout = self.config.deadline_seconds if remaining is None else min(self.config.deadline_seconds, remaining)
            async with asyncio.timeout(timeout):
                response = await client.chat.completions.create(**options)
            usage = getattr(response, "usage", None)
            details = getattr(usage, "completion_tokens_details", None)
            record.update(status="RECEIVED", input_tokens=getattr(usage, "prompt_tokens", None),
                          output_tokens=getattr(usage, "completion_tokens", None),
                          reasoning_tokens=getattr(details, "reasoning_tokens", None),
                          reported_model=getattr(response, "model", None),
                          finish_reason=getattr(response.choices[0], "finish_reason", None) if response.choices else None)
            return response
        except BaseException as exc:
            record.update(status="FAILED", error_category=type(exc).__name__)
            raise
        finally:
            record["latency_ms"] = round((time.perf_counter() - started) * 1000, 2)
            if sink:
                await sink(dict(record))

    async def stream(self, client, *, messages, response_format, max_tokens):
        """Account for one request through normal completion or interrupted IO."""
        options = self.request_options(messages=messages, response_format=response_format, max_tokens=max_tokens)
        record = {"call_id": uuid4().hex, "provider": self.config.provider,
                  "requested_model": options["model"], "reasoning_effort": options.get("reasoning_effort", "none"),
                  "output_mode": options["response_format"]["type"], "max_output_tokens": options["max_tokens"],
                  "status": "DISPATCHING", "input_tokens": None, "output_tokens": None,
                  "reasoning_tokens": None, "reported_model": None, "finish_reason": None}
        sink = _usage_sink.get()
        if sink:
            await sink(dict(record))
        self.calls.append(record)
        started = time.perf_counter()
        stream = None
        try:
            from app.trip_understanding.inference_allowance import remaining_call_seconds
            remaining = remaining_call_seconds()
            timeout = self.config.deadline_seconds if remaining is None else min(self.config.deadline_seconds, remaining)
            async with asyncio.timeout(timeout):
                stream = await client.chat.completions.create(**options, stream=True,
                                                            stream_options={"include_usage": True})
                async for chunk in stream:
                    record["reported_model"] = getattr(chunk, "model", None) or record["reported_model"]
                    usage = getattr(chunk, "usage", None)
                    if usage is not None:
                        record.update(input_tokens=usage.prompt_tokens, output_tokens=usage.completion_tokens,
                            reasoning_tokens=getattr(getattr(usage, "completion_tokens_details", None), "reasoning_tokens", None))
                    for choice in chunk.choices:
                        if choice.index != 0:
                            continue
                        if choice.finish_reason is not None:
                            record["finish_reason"] = choice.finish_reason
                        if choice.delta.content:
                            record.setdefault("first_content_ms", round((time.perf_counter() - started) * 1000, 2))
                            yield choice.delta.content
            if record["finish_reason"] != "stop":
                raise ValueError("STREAM_NOT_COMPLETED")
            record["status"] = "RECEIVED"
        except BaseException as exc:
            record.update(status="FAILED", error_category=type(exc).__name__)
            raise
        finally:
            try:
                if stream is not None:
                    await stream.close()
            finally:
                record["latency_ms"] = round((time.perf_counter() - started) * 1000, 2)
                if sink:
                    await sink(dict(record))


def usage_summary(calls):
    result = {"external_calls": len(calls)}
    for field in ("input_tokens", "output_tokens", "reasoning_tokens"):
        known = [call[field] for call in calls if isinstance(call.get(field), int)]
        result[field] = sum(known) if known else None
        result[field + "_unknown_calls"] = len(calls) - len(known)
    return result
