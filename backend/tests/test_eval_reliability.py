import asyncio
import json
import time

import pytest

from app.config import Settings
from app.rag.execution_policy import select_rag_policy
from app.tools.runtime import ProviderRuntime, ToolCallEnvelope, ToolRuntimeError, enforce_tool_budget
from evals.experiments import ExperimentConfig, ExperimentRunner
from evals.faults import inject_fault
from evals.metrics import bootstrap_ci, ndcg_at_k, precision_recall_f1, reciprocal_rank
from evals.runner import load_cases
from evals.schema import EvalSplit


def test_blind_labels_cannot_be_loaded_for_tuning():
    from pathlib import Path
    with pytest.raises(PermissionError):
        load_cases(Path("eval_data/router/blind.jsonl"), purpose="experiment_tuning", split=EvalSplit.BLIND)


def test_metrics_are_deterministic_and_rank_aware():
    first = bootstrap_ci([0, 1, 1, 1], seed=42, iterations=100)
    second = bootstrap_ci([0, 1, 1, 1], seed=42, iterations=100)
    assert first == second
    assert precision_recall_f1({"a", "b"}, {"b", "c"}) == (0.5, 0.5, 0.5)
    assert reciprocal_rank("b", ["a", "b"]) == 0.5
    assert ndcg_at_k({"b"}, ["a", "b"], 2) < 1


def test_experiment_cache_is_configuration_scoped_and_pareto_is_correct():
    runner = ExperimentRunner()
    calls = []
    one = ExperimentConfig(reranker=False)
    two = ExperimentConfig(reranker=True)
    assert runner.run(one, lambda cfg: calls.append(cfg.cache_key) or {"value": 1}) == runner.run(
        one, lambda _cfg: {"value": 999}
    )
    runner.run(two, lambda cfg: calls.append(cfg.cache_key) or {"value": 2})
    assert len(calls) == 2
    rows = [
        {"name": "dominated", "quality": 0.7, "p95_latency_ms": 20, "cost_usd": 1},
        {"name": "frontier", "quality": 0.8, "p95_latency_ms": 10, "cost_usd": 1},
    ]
    assert [item["name"] for item in runner.pareto(rows)] == ["frontier"]


def test_dynamic_rag_policy_has_explainable_boundaries():
    settings = Settings(hyde_enabled=True, multi_query_enabled=False, reranker_enabled=True, reranker_min_candidates=8)
    precise = select_rag_policy("西湖", "scenic", 20, settings)
    assert not precise.use_hyde and not precise.use_multi_query and precise.use_reranker
    descriptive = select_rag_policy("杭州酒店住宿避坑和交通经验", "hotel", 5, settings)
    assert descriptive.use_multi_query and not descriptive.use_hyde and not descriptive.use_reranker


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "profile",
    ["deepseek_timeout", "deepseek_429", "deepseek_5xx", "invalid_model_json"],
)
async def test_synthesizer_provider_faults_preserve_grounded_poi(profile):
    result = await inject_fault(profile)
    assert result["passed"] is True
    assert result["preserved_place_ids"] == ["p1"]
    assert result["response_nonempty"] is True


@pytest.mark.asyncio
async def test_tool_runtime_rejects_unknown_tool_and_honours_total_deadline():
    runtime = ProviderRuntime()
    envelope = ToolCallEnvelope(
        trace_id="test", actor_user_id="user", tool="unknown", arguments={},
        authorization_scope=set(), deadline_monotonic=time.monotonic() + 1,
        idempotency_key="test:unknown",
    )
    with pytest.raises(ToolRuntimeError):
        await runtime.execute(envelope, lambda _: asyncio.sleep(0))
    accepted, rejected = enforce_tool_budget([{"id": index} for index in range(5)], 3)
    assert len(accepted) == 3 and len(rejected) == 2


def test_fault_evidence_contains_all_fixed_cases():
    report = json.loads(open("evidence/fault_injection/summary.json", encoding="utf-8").read())
    assert report["sample_size"] == 24
    assert len({item["fault_profile"] for item in report["results"]}) == 24
    assert all(item["injection"].startswith("controlled:") for item in report["results"])
