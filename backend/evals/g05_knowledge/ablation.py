from __future__ import annotations

import json
import math
import statistics
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from app.trip_understanding.knowledge import (
    KnowledgeClaimCandidate,
    project_knowledge_suggestions,
)
from app.trip_understanding.knowledge_admin import canonical_json_hash
from app.trip_understanding.models import (
    ActivityCardView,
    AssumptionChipView,
    MapReadinessView,
    StaySuggestionView,
    TripDayView,
    UserFacingTripResult,
)


@dataclass(frozen=True)
class AblationReport:
    dataset_id: str
    case_count: int
    shown_count: int
    supported_count: int
    unsupported_count: int
    precision: float
    baseline_actionability: float
    knowledge_actionability: float
    actionability_lift_percentage_points: float
    baseline_p95_ms: float
    knowledge_p95_ms: float
    p95_regression: float
    authoritative_field_changes: int
    passed: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_ablation_oracle(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _candidate(raw: dict[str, Any], source: dict[str, Any]) -> KnowledgeClaimCandidate:
    return KnowledgeClaimCandidate(
        claim_revision_id=raw["claim_key"],
        claim_key=raw["claim_key"],
        claim_version=int(raw["version"]),
        canonical_place_id=raw["canonical_place_id"],
        claim_type=raw["claim_type"],
        conditions_hash=canonical_json_hash(raw["conditions"]),
        suggestion_text=raw["suggestion_text"],
        short_evidence=raw["short_evidence"],
        effective_at=datetime.fromisoformat(raw["effective_at"]),
        expires_at=datetime.fromisoformat(raw["expires_at"]),
        source_version_id=f"{source['source_key']}:v{source['version']}",
        source_name=source["publisher_name"],
        source_url=source["canonical_url"],
        source_observed_at=datetime.fromisoformat(source["observed_at"]),
        source_expires_at=datetime.fromisoformat(source["expires_at"]),
        source_admission_status=source["admission_status"],
        source_license_status=source["license_status"],
    )


def _fixture_result(manifest: dict[str, Any]) -> tuple[UserFacingTripResult, dict[str, str | None]]:
    cards: list[ActivityCardView] = []
    bindings: dict[str, str | None] = {}
    for index, place in enumerate(manifest["places"]):
        token = f"g05-ablation-activity-{index:03d}"
        place_id = place["canonical_place_id"]
        bindings[token] = place_id
        cards.append(
            ActivityCardView(
                activity_token=token,
                name=place["canonical_name"],
                category="景点",
                area_or_address=place["city"],
                status="READY" if place_id else "NEEDS_CONFIRMATION",
                available_actions=["VIEW_DETAILS", "REPLACE", "DELETE", "MOVE"],
            )
        )
    result = UserFacingTripResult(
        status="PARTIAL_RESULT",
        assumptions=[
            AssumptionChipView(
                key="party_size",
                label="人数",
                value="2 人",
                editable=True,
            )
        ],
        days=[TripDayView(label="冻结三城验证集", activities=cards)],
        map=MapReadinessView(status="PREPARING", message="路线准备中"),
        stay=StaySuggestionView(status="PREPARING", message="住宿建议准备中"),
        available_actions=["EDIT_ASSUMPTIONS", "EDIT_CARDS"],
    )
    return result, bindings


def _p95(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)]


def _benchmark(
    result: UserFacingTripResult,
    bindings: dict[str, str | None],
    candidates: list[KnowledgeClaimCandidate],
    *,
    as_of: datetime,
    samples: int,
    rounds: int,
) -> tuple[float, float]:
    def execute(active: list[KnowledgeClaimCandidate]) -> None:
        projection = project_knowledge_suggestions(
            result,
            canonical_place_by_activity_token=bindings,
            candidates=active,
            now=as_of,
        )
        projection.result.model_dump_json()

    for _ in range(50):
        execute([])
        execute(candidates)

    baseline_rounds: list[float] = []
    knowledge_rounds: list[float] = []
    for round_index in range(rounds):
        baseline: list[float] = []
        knowledge: list[float] = []
        order = ([], candidates) if round_index % 2 == 0 else (candidates, [])
        for _ in range(samples):
            measured: dict[bool, float] = {}
            for active in order:
                started = time.perf_counter_ns()
                execute(active)
                measured[bool(active)] = (time.perf_counter_ns() - started) / 1_000_000
            baseline.append(measured[False])
            knowledge.append(measured[True])
        baseline_rounds.append(_p95(baseline))
        knowledge_rounds.append(_p95(knowledge))
    return statistics.median(baseline_rounds), statistics.median(knowledge_rounds)


def evaluate_knowledge_ablation(
    manifest: dict[str, Any],
    oracle: dict[str, Any],
    *,
    as_of: datetime,
    samples: int = 300,
    rounds: int = 5,
) -> AblationReport:
    if manifest.get("dataset_id") != oracle.get("dataset_id"):
        raise ValueError("admission manifest and ablation oracle dataset bindings differ")
    places = {place["place_key"]: place for place in manifest["places"]}
    cases = oracle["cases"]
    if set(places) != {case["place_key"] for case in cases} or len(cases) != 18:
        raise ValueError("ablation oracle must bind all 18 frozen places exactly once")
    sources = {
        (source["source_key"], int(source["version"])): source
        for source in manifest["sources"]
    }
    candidates = [
        _candidate(raw, sources[(raw["source_key"], int(raw["source_version"]))])
        for raw in manifest["claims"]
    ]
    result, bindings = _fixture_result(manifest)
    baseline = project_knowledge_suggestions(
        result,
        canonical_place_by_activity_token=bindings,
        candidates=[],
        now=as_of,
    ).result
    knowledge = project_knowledge_suggestions(
        result,
        canonical_place_by_activity_token=bindings,
        candidates=candidates,
        now=as_of,
    ).result

    oracle_by_place = {case["place_key"]: case for case in cases}
    place_key_by_name = {
        place["canonical_name"]: place["place_key"]
        for place in manifest["places"]
    }
    shown_count = 0
    supported_count = 0
    knowledge_actionable_places = 0
    for card in knowledge.days[0].activities:
        case = oracle_by_place[place_key_by_name[card.name]]
        if card.knowledge_suggestions:
            knowledge_actionable_places += 1
        for suggestion in card.knowledge_suggestions:
            shown_count += 1
            if suggestion.type in case["allowed_types"] and case["actionable_expected"]:
                supported_count += 1
    baseline_actionable_places = sum(
        bool(card.knowledge_suggestions)
        for day in baseline.days
        for card in day.activities
    )
    case_count = len(cases)
    unsupported_count = shown_count - supported_count
    precision = supported_count / shown_count if shown_count else 0.0
    baseline_actionability = baseline_actionable_places / case_count
    knowledge_actionability = knowledge_actionable_places / case_count
    lift = (knowledge_actionability - baseline_actionability) * 100

    stripped = knowledge.model_copy(deep=True)
    for day in stripped.days:
        for card in day.activities:
            card.knowledge_suggestions = []
    authoritative_field_changes = 0 if stripped == result else 1
    baseline_p95, knowledge_p95 = _benchmark(
        result,
        bindings,
        candidates,
        as_of=as_of,
        samples=samples,
        rounds=rounds,
    )
    regression = (knowledge_p95 - baseline_p95) / baseline_p95
    passed = (
        precision >= 0.90
        and unsupported_count == 0
        and lift >= 5.0
        and regression <= 0.20
        and authoritative_field_changes == 0
    )
    return AblationReport(
        dataset_id=manifest["dataset_id"],
        case_count=case_count,
        shown_count=shown_count,
        supported_count=supported_count,
        unsupported_count=unsupported_count,
        precision=precision,
        baseline_actionability=baseline_actionability,
        knowledge_actionability=knowledge_actionability,
        actionability_lift_percentage_points=lift,
        baseline_p95_ms=baseline_p95,
        knowledge_p95_ms=knowledge_p95,
        p95_regression=regression,
        authoritative_field_changes=authoritative_field_changes,
        passed=passed,
    )
