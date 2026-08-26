"""Low-cardinality Prometheus-compatible application metrics."""

from __future__ import annotations

import threading
from collections import defaultdict
from typing import Iterable


_FORBIDDEN_LABELS = {"user_id", "room_id", "query", "prompt", "message", "token", "trace_id"}
_ALLOWED_LABELS = {"status", "tool", "provider", "error_category", "constraint_type", "reason", "model", "profile"}


class MetricsRegistry:
    def __init__(self):
        self._lock = threading.Lock()
        self._counters = defaultdict(float)
        self._histograms: dict[tuple, list[float]] = defaultdict(list)

    @staticmethod
    def _labels(labels: dict[str, str]):
        if _FORBIDDEN_LABELS & labels.keys() or not labels.keys() <= _ALLOWED_LABELS:
            raise ValueError("high-cardinality or unsupported metric label")
        return tuple(sorted((key, str(value)) for key, value in labels.items()))

    def inc(self, name: str, amount: float = 1.0, **labels: str) -> None:
        key = (name, self._labels(labels))
        with self._lock:
            self._counters[key] += amount

    def observe(self, name: str, value: float, **labels: str) -> None:
        key = (name, self._labels(labels))
        with self._lock:
            self._histograms[key].append(float(value))

    def render(self) -> str:
        lines = []
        with self._lock:
            counters = dict(self._counters)
            histograms = {key: list(values) for key, values in self._histograms.items()}
        for (name, labels), value in sorted(counters.items()):
            lines.append(f"{name}{_render_labels(labels)} {value:g}")
        for (name, labels), values in sorted(histograms.items()):
            if not values:
                continue
            base = _render_labels(labels)
            lines.append(f"{name}_count{base} {len(values)}")
            lines.append(f"{name}_sum{base} {sum(values):g}")
            lines.append(f"{name}_max{base} {max(values):g}")
        return "\n".join(lines) + "\n"

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._histograms.clear()


def _render_labels(labels: Iterable[tuple[str, str]]) -> str:
    values = list(labels)
    if not values:
        return ""
    escaped = [f'{key}="{value.replace(chr(92), chr(92) * 2).replace(chr(34), chr(92) + chr(34))}"' for key, value in values]
    return "{" + ",".join(escaped) + "}"


metrics = MetricsRegistry()


CORE_METRIC_NAMES = {
    "agent_request_total", "agent_task_completed_total", "agent_degraded_total",
    "agent_duration_seconds", "agent_time_to_first_meaningful_place_seconds",
    "agent_tool_duration_seconds", "agent_tool_failure_total", "agent_react_iterations",
    "agent_llm_input_tokens", "agent_llm_output_tokens", "agent_estimated_cost_usd",
    "rag_retrieval_duration_seconds", "rag_empty_result_total", "constraint_check_total",
    "constraint_unknown_total", "constraint_false_pass_detected_total", "sse_disconnect_total",
    "memory_write_total", "memory_write_rejected_total", "yjs_connection_total",
}
