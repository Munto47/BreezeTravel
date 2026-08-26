from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from openai import AsyncOpenAI


@dataclass(frozen=True)
class StructuredJsonReceipt:
    requested_model: str
    actual_model: str | None
    input_tokens: int
    output_tokens: int
    latency_ms: float
    finish_reason: str | None
    system_fingerprint: str | None


@dataclass(frozen=True)
class StructuredJsonResult:
    payload: dict[str, Any]
    receipt: StructuredJsonReceipt


class StructuredExtractionClientError(RuntimeError):
    def __init__(self, category: str, receipt: StructuredJsonReceipt):
        super().__init__(category)
        self.category = category
        self.receipt = receipt


def _error_category(exc: Exception) -> str:
    name = type(exc).__name__.lower()
    status_code = getattr(exc, "status_code", None)
    if "timeout" in name:
        return "timeout"
    if status_code == 429 or "ratelimit" in name:
        return "rate_limit"
    if isinstance(status_code, int) and status_code >= 500:
        return "server_error"
    return "client_error"


class DeepSeekJsonClient:
    """One-shot, no-tool DeepSeek JSON client with retries disabled."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        timeout_seconds: float = 4.5,
        max_output_tokens: int = 4096,
        sdk_client: Any | None = None,
    ) -> None:
        if not api_key.strip():
            raise ValueError("DeepSeek API key is required")
        self.timeout_seconds = timeout_seconds
        self.max_output_tokens = max_output_tokens
        self._client = sdk_client or AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout_seconds,
            max_retries=0,
        )

    async def generate_json(
        self,
        *,
        system_prompt: str,
        input_payload: dict[str, Any],
        json_schema: dict[str, Any],
        model_name: str,
        temperature: float,
    ) -> StructuredJsonResult:
        started = time.perf_counter()
        try:
            response = await self._client.chat.completions.create(
                model=model_name,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            f"{system_prompt}\n"
                            "只返回一个 JSON 对象，不要 Markdown、解释或思维过程。"
                            "输出必须符合下面的 JSON Schema：\n"
                            f"{json.dumps(json_schema, ensure_ascii=False, separators=(',', ':'))}"
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps(
                            input_payload,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                    },
                ],
                response_format={"type": "json_object"},
                temperature=temperature,
                max_tokens=self.max_output_tokens,
                stream=False,
                timeout=self.timeout_seconds,
                extra_body={"thinking": {"type": "disabled"}},
            )
        except Exception as exc:
            receipt = StructuredJsonReceipt(
                requested_model=model_name,
                actual_model=None,
                input_tokens=0,
                output_tokens=0,
                latency_ms=(time.perf_counter() - started) * 1000,
                finish_reason=None,
                system_fingerprint=None,
            )
            raise StructuredExtractionClientError(_error_category(exc), receipt) from exc

        latency_ms = (time.perf_counter() - started) * 1000
        choice = response.choices[0] if response.choices else None
        usage = getattr(response, "usage", None)
        receipt = StructuredJsonReceipt(
            requested_model=model_name,
            actual_model=getattr(response, "model", None),
            input_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
            latency_ms=latency_ms,
            finish_reason=getattr(choice, "finish_reason", None),
            system_fingerprint=getattr(response, "system_fingerprint", None),
        )
        if choice is None:
            raise StructuredExtractionClientError("empty_output", receipt)
        if choice.finish_reason == "length":
            raise StructuredExtractionClientError("truncated_output", receipt)
        content = choice.message.content
        if not isinstance(content, str) or not content.strip():
            raise StructuredExtractionClientError("empty_output", receipt)
        try:
            payload = json.loads(content)
        except json.JSONDecodeError as exc:
            raise StructuredExtractionClientError("invalid_json", receipt) from exc
        if not isinstance(payload, dict):
            raise StructuredExtractionClientError("invalid_json", receipt)
        return StructuredJsonResult(payload=payload, receipt=receipt)
