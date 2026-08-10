"""Run reproducible local ablations over frozen public and deterministic sets."""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import jieba
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

os.environ.setdefault("RUNTIME_PROFILE", "test")
os.environ.setdefault("DEMO_MODE", "true")
os.environ.setdefault("PLACE_META_LOOKUP_ENABLED", "false")

from app.agents.planner.repair_controller import TargetedRepairController
from app.agents.routing_policy import plan_simple_tools, plan_tools
from app.constraints.verifier import ItineraryVerifier
from evals.adapters import _verifier_fixture
from evals.experiments import ExperimentConfig, ExperimentRunner
from evals.metrics import ndcg_at_k, percentile, reciprocal_rank
from evals.runner import canonical_hash, load_cases
from evals.schema import EvalKind, EvalSplit


BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent


def commit_sha() -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()


def tokens(text: str) -> list[str]:
    return [item.lower() for item in jieba.lcut(re.sub(r"[^\w\u4e00-\u9fff]+", " ", text)) if item.strip()]


def bm25_scores(query: str, documents: list[str]) -> list[float]:
    query_terms = tokens(query)
    doc_terms = [tokens(item) for item in documents]
    average_length = sum(map(len, doc_terms)) / max(1, len(doc_terms))
    document_frequency = Counter(term for terms in doc_terms for term in set(terms))
    scores = []
    for terms in doc_terms:
        counts = Counter(terms)
        score = 0.0
        for term in query_terms:
            df = document_frequency.get(term, 0)
            inverse = math.log(1 + (len(doc_terms) - df + 0.5) / (df + 0.5))
            frequency = counts.get(term, 0)
            denominator = frequency + 1.2 * (1 - 0.75 + 0.75 * len(terms) / max(1, average_length))
            score += inverse * frequency * 2.2 / denominator if denominator else 0
        scores.append(score)
    return scores


def rrf(*rankings: list[int], k: int = 60) -> list[int]:
    scores = Counter()
    for ranking in rankings:
        for rank, index in enumerate(ranking, start=1):
            scores[index] += 1 / (k + rank)
    return [index for index, _ in scores.most_common()]


def expand_query(query: str, variant: str) -> list[str]:
    if variant == "multi_query":
        expansions = [query]
        synonym_pairs = (("酒店", "住宿"), ("餐厅", "美食"), ("交通", "地铁"), ("避坑", "注意事项"))
        for left, right in synonym_pairs:
            if left in query:
                expansions.append(query.replace(left, right))
            elif right in query:
                expansions.append(query.replace(right, left))
        return expansions[:3]
    if variant == "hyde":
        return [query + " 旅行攻略 地点事实 交通 预约 注意事项"]
    return [query]


def retrieval_ablation(variant: str, sources: list[dict], cases: list[dict]) -> dict:
    started = time.perf_counter()
    raw = []
    latencies = []
    for case in cases:
        case_started = time.perf_counter()
        candidates = [item for item in sources if item["city"] == case["city"]]
        docs = [f"{item['title']} {item['content']}" for item in candidates]
        queries = expand_query(case["question"], variant)
        dense_rankings, sparse_rankings = [], []
        for query in queries:
            vectorizer = TfidfVectorizer(analyzer="char", ngram_range=(2, 4), min_df=1)
            matrix = vectorizer.fit_transform(docs + [query])
            dense_scores = cosine_similarity(matrix[-1], matrix[:-1]).ravel().tolist()
            dense_rankings.append(sorted(range(len(docs)), key=lambda index: dense_scores[index], reverse=True))
            sparse_scores = bm25_scores(query, docs)
            sparse_rankings.append(sorted(range(len(docs)), key=lambda index: sparse_scores[index], reverse=True))
        if variant == "dense":
            ranking = dense_rankings[0]
        elif variant == "bm25":
            ranking = sparse_rankings[0]
        else:
            ranking = rrf(*(dense_rankings + sparse_rankings))
        if variant == "rrf_reranker":
            query_terms = set(tokens(case["question"]))
            ranking = sorted(
                ranking[:10],
                key=lambda index: len(query_terms & set(tokens(docs[index]))) / max(1, len(query_terms)),
                reverse=True,
            ) + ranking[10:]
        ranked_ids = [candidates[index]["id"] for index in ranking]
        latency = (time.perf_counter() - case_started) * 1000
        latencies.append(latency)
        raw.append({
            "case_id": case["id"], "city": case["city"], "expected_source_id": case["expected_source_id"],
            "ranked_source_ids": ranked_ids[:5], "hit_at_5": case["expected_source_id"] in ranked_ids[:5],
            "reciprocal_rank": reciprocal_rank(case["expected_source_id"], ranked_ids),
            "ndcg_at_5": ndcg_at_k({case["expected_source_id"]}, ranked_ids, 5),
            "citation_support": all(candidates[index].get("source_url") and candidates[index].get("source_license") for index in ranking[:5]),
            "latency_ms": latency,
        })
    return {
        "component": "offline_in_memory_public_retrieval_proxy", "variant": variant,
        "sample_size": len(raw), "recall_at_5": sum(item["hit_at_5"] for item in raw) / len(raw),
        "mrr": sum(item["reciprocal_rank"] for item in raw) / len(raw),
        "ndcg_at_5": sum(item["ndcg_at_5"] for item in raw) / len(raw),
        "citation_support": sum(item["citation_support"] for item in raw) / len(raw),
        "p50_latency_ms": percentile(latencies, 0.5), "p95_latency_ms": percentile(latencies, 0.95),
        "cost_usd": 0.0, "quality": sum(item["hit_at_5"] for item in raw) / len(raw),
        "duration_seconds": time.perf_counter() - started, "raw_cases": raw,
        "claim_boundary": "in-memory ablation; not the PostgreSQL pgvector production benchmark",
    }


def router_ablation(variant: str, cases) -> dict:
    raw, latencies = [], []
    for case in cases:
        started = time.perf_counter()
        if variant == "deterministic_policy":
            plan = plan_tools(case.input["query"]) or plan_simple_tools(case.input["query"])
            tools = list(plan.tools) if plan else []
        else:
            tools = ["search_places"]
        latency = (time.perf_counter() - started) * 1000
        latencies.append(latency)
        passed = set(tools) == set(case.expected["tool_set"])
        raw.append({"case_id": case.id, "expected": case.expected["tool_set"], "actual": tools, "passed": passed, "latency_ms": latency})
    quality = sum(item["passed"] for item in raw) / len(raw)
    return {
        "component": "router", "variant": variant, "sample_size": len(raw),
        "tool_set_exact_match": quality, "quality": quality,
        "p50_latency_ms": percentile(latencies, .5), "p95_latency_ms": percentile(latencies, .95),
        "cost_usd": 0.0, "raw_cases": raw,
    }


def planner_ablation(variant: str, cases) -> dict:
    raw, latencies = [], []
    for case in cases:
        started = time.perf_counter()
        task, plan, places = _verifier_fixture(case.input["fixture_profile"])
        verifier = ItineraryVerifier()
        initial = verifier.verify(task, plan, places=places)
        report = initial
        rounds = 0
        if variant == "verifier_repair":
            controller = TargetedRepairController()
            while rounds < controller.max_rounds:
                repairable = [item for item in report.checks if item.status.value == "VIOLATED" and item.repairable]
                if not repairable:
                    break
                plan, _repair_plan = controller.repair_once(plan, task, report.checks, places)
                rounds += 1
                report = verifier.verify(task, plan, places=places, repair_rounds=rounds)
        violation_count = sum(item.status.value == "VIOLATED" for item in report.checks)
        unknown_count = sum(item.status.value == "UNKNOWN" for item in report.checks)
        initial_violations = sum(item.status.value == "VIOLATED" for item in initial.checks)
        latency = (time.perf_counter() - started) * 1000
        latencies.append(latency)
        raw.append({
            "case_id": case.id, "profile": case.input["fixture_profile"],
            "initial_violations": initial_violations, "final_violations": violation_count,
            "unknown_count": unknown_count, "repair_rounds": rounds, "latency_ms": latency,
        })
    total_checks = sum(max(1, item["initial_violations"]) for item in raw)
    residual = sum(item["final_violations"] for item in raw)
    quality = 1 - residual / total_checks
    return {
        "component": "planner_constraint_layer", "variant": variant, "sample_size": len(raw),
        "constraint_satisfaction_gain": quality, "quality": quality,
        "false_pass_count": sum(item["initial_violations"] > 0 for item in raw) if variant == "without_verifier" else 0,
        "unknown_rate": sum(item["unknown_count"] > 0 for item in raw) / len(raw),
        "p50_latency_ms": percentile(latencies, .5), "p95_latency_ms": percentile(latencies, .95),
        "cost_usd": 0.0, "raw_cases": raw,
    }


def main() -> None:
    manifest = json.loads((BACKEND / "eval_data" / "manifest.json").read_text(encoding="utf-8"))
    sources = [json.loads(line) for line in (BACKEND / "data" / "generated" / "public_sources.jsonl").read_text(encoding="utf-8").splitlines() if line]
    public_cases = json.loads((BACKEND / "evidence" / "corpus" / "public_eval_cases.json").read_text(encoding="utf-8"))["cases"]
    blind_rag = [item for item in public_cases if item["split"] == "blind"]
    router_cases = sum((load_cases(BACKEND / "eval_data" / "router" / f"{split.value}.jsonl", purpose="final_evaluation", split=split) for split in EvalSplit), [])
    verifier_cases = sum((load_cases(BACKEND / "eval_data" / "verifier" / f"{split.value}.jsonl", purpose="final_evaluation", split=split) for split in EvalSplit), [])

    runner = ExperimentRunner()
    rows = []
    for variant in ("dense", "bm25", "rrf", "rrf_reranker", "hyde", "multi_query"):
        config = ExperimentConfig(
            retrieval=variant, reranker=variant == "rrf_reranker", hyde=variant == "hyde",
            multi_query=variant == "multi_query", agent_mode="not_applicable", router="not_applicable",
            planner="not_applicable", corpus_hash=canonical_hash(sources), dataset_hash=canonical_hash(blind_rag),
            prompt_version="offline-public-retrieval-v1",
        )
        row = runner.run(config, lambda _cfg, v=variant: retrieval_ablation(v, sources, blind_rag))
        rows.append({"config_hash": config.cache_key, **row["metrics"]})
    for variant in ("always_amap_baseline", "deterministic_policy"):
        config = ExperimentConfig(retrieval="not_applicable", agent_mode="routing_only", router=variant, planner="not_applicable", dataset_hash=manifest["datasets"][EvalKind.ROUTER.value]["hash"])
        row = runner.run(config, lambda _cfg, v=variant: router_ablation(v, router_cases))
        rows.append({"config_hash": config.cache_key, **row["metrics"]})
    for variant in ("without_verifier", "verifier_repair"):
        config = ExperimentConfig(retrieval="not_applicable", agent_mode="fixed_planner", router="not_applicable", planner=variant, dataset_hash=manifest["datasets"][EvalKind.VERIFIER.value]["hash"])
        row = runner.run(config, lambda _cfg, v=variant: planner_ablation(v, verifier_cases))
        rows.append({"config_hash": config.cache_key, **row["metrics"]})

    comparable = [item for item in rows if item["component"] in {"router", "planner_constraint_layer"}]
    frontier = ExperimentRunner.pareto(comparable)
    report = {
        "schema_version": "1.0", "generated_at": datetime.now(timezone.utc).isoformat(),
        "commit_sha": commit_sha(), "environment": "local-controlled-cpu",
        "seed": 42, "cache_state": "fresh_process_memory_cache", "pricing_version": "no-LLM-cost-local-v1",
        "rows": rows, "pareto": [{"component": item["component"], "variant": item["variant"], "quality": item["quality"], "p95_latency_ms": item["p95_latency_ms"], "cost_usd": item["cost_usd"]} for item in frontier],
        "dynamic_policy_adopted": {
            "precise_query": "skip HyDE and Multi-Query",
            "food_hotel_tips_descriptive": "enable Multi-Query",
            "reranker": "only when candidate_count reaches configured threshold",
            "mixed_live_and_evidence": "force Amap plus RAG",
            "simple_one_step": "deterministic workflow",
            "open_multi_step": "bounded ReAct",
            "planner": "Verifier plus at most two targeted repair rounds",
        },
        "not_executed": [
            {"variant": "API LLM Router", "reason": "would consume external provider quota; no current local-real run was authorised as a production claim"},
            {"variant": "LoRA Router current rerun", "reason": "requires local GPU/model runtime; historical artifact is not relabelled as current"},
            {"variant": "fixed workflow vs ReAct vs ReAct+Critic with external LLM", "reason": "requires external provider; deterministic routing and planner layers were evaluated separately"},
        ],
        "production_claim_not_made": True,
    }
    target = BACKEND / "evidence" / "experiments" / "summary.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(target)
    print(json.dumps({"rows": len(rows), "pareto": report["pareto"], "not_executed": len(report["not_executed"])}, ensure_ascii=False))


if __name__ == "__main__":
    main()
