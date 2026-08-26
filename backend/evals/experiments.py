from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from evals.runner import canonical_hash


@dataclass(frozen=True)
class ExperimentConfig:
    retrieval: str = "dense_bm25_rrf"
    reranker: bool = True
    hyde: bool = True
    multi_query: bool = True
    agent_mode: str = "react_critic"
    router: str = "rule_llm"
    planner: str = "verifier_repair"
    seed: int = 42
    model: str = "unconfigured"
    prompt_version: str = "v1"
    corpus_hash: str = "unconfigured"
    dataset_hash: str = "unconfigured"
    pricing_version: str = "unconfigured"

    @property
    def cache_key(self) -> str:
        return canonical_hash(self.__dict__)


class ExperimentRunner:
    def __init__(self):
        self._cache: dict[str, dict[str, Any]] = {}

    def run(self, config: ExperimentConfig, evaluator: Callable[[ExperimentConfig], dict[str, Any]]):
        key = config.cache_key
        if key not in self._cache:
            self._cache[key] = {"config": config.__dict__, "metrics": evaluator(config)}
        return self._cache[key]

    @staticmethod
    def pareto(rows: list[dict[str, Any]], quality="quality", latency="p95_latency_ms", cost="cost_usd"):
        frontier = []
        for candidate in rows:
            dominated = any(
                other is not candidate
                and other[quality] >= candidate[quality]
                and other[latency] <= candidate[latency]
                and other[cost] <= candidate[cost]
                and (other[quality] > candidate[quality] or other[latency] < candidate[latency] or other[cost] < candidate[cost])
                for other in rows
            )
            if not dominated:
                frontier.append(candidate)
        return frontier
