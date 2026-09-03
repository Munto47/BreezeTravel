from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from typing import Iterable, Literal

from pydantic import BaseModel, ConfigDict

from app.trip_understanding.models import KnowledgeSuggestionView, UserFacingTripResult


ADMITTED_LICENSE_STATES = {"FACTS_ONLY_WITH_ATTRIBUTION", "OPEN_DATA_REUSE"}
CLAIM_TYPE_PRIORITY = {
    "RESERVATION_ADVICE": 0,
    "TYPICAL_DURATION": 1,
    "SUITABLE_TIME": 2,
    "NIGHT_VIEW": 3,
    "SEASON": 4,
}


class KnowledgeClaimCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    claim_revision_id: str
    claim_key: str
    claim_version: int
    canonical_place_id: str
    claim_type: Literal[
        "TYPICAL_DURATION",
        "SUITABLE_TIME",
        "NIGHT_VIEW",
        "SEASON",
        "RESERVATION_ADVICE",
    ]
    conditions_hash: str
    suggestion_text: str
    short_evidence: str
    effective_at: datetime
    expires_at: datetime
    claim_withdrawn_at: datetime | None = None
    source_version_id: str
    source_name: str
    source_url: str
    source_observed_at: datetime
    source_expires_at: datetime
    source_admission_status: str
    source_license_status: str
    source_withdrawn_at: datetime | None = None
    is_latest_claim_version: bool = True
    is_latest_source_version: bool = True


@dataclass(frozen=True)
class KnowledgeProjection:
    result: UserFacingTripResult
    selected_claim_revision_ids: tuple[str, ...]
    conflicted_groups: tuple[tuple[str, str, str], ...]


def _active(candidate: KnowledgeClaimCandidate, *, now: datetime) -> bool:
    return (
        candidate.is_latest_claim_version
        and candidate.is_latest_source_version
        and candidate.source_admission_status == "ADMITTED"
        and candidate.source_license_status in ADMITTED_LICENSE_STATES
        and (
            candidate.claim_withdrawn_at is None
            or candidate.claim_withdrawn_at > now
        )
        and (
            candidate.source_withdrawn_at is None
            or candidate.source_withdrawn_at > now
        )
        and candidate.effective_at <= now < candidate.expires_at
        and candidate.source_observed_at <= now < candidate.source_expires_at
        and candidate.source_url.startswith("https://")
    )


@lru_cache(maxsize=1024)
def _select_knowledge_candidates_cached(
    candidates: tuple[KnowledgeClaimCandidate, ...],
    *,
    now: datetime,
    max_per_place: int = 3,
) -> tuple[dict[str, tuple[KnowledgeClaimCandidate, ...]], tuple[tuple[str, str, str], ...]]:
    active = [candidate for candidate in candidates if _active(candidate, now=now)]
    groups: dict[tuple[str, str, str], list[KnowledgeClaimCandidate]] = defaultdict(list)
    for candidate in active:
        groups[
            (
                candidate.canonical_place_id,
                candidate.claim_type,
                candidate.conditions_hash,
            )
        ].append(candidate)

    conflict_groups: list[tuple[str, str, str]] = []
    accepted: dict[str, list[KnowledgeClaimCandidate]] = defaultdict(list)
    for group_key, grouped in groups.items():
        meanings = {
            (candidate.suggestion_text.strip(), candidate.short_evidence.strip())
            for candidate in grouped
        }
        if len(meanings) > 1:
            conflict_groups.append(group_key)
            continue
        chosen = max(
            grouped,
            key=lambda candidate: (
                candidate.source_observed_at,
                candidate.claim_version,
                candidate.claim_revision_id,
            ),
        )
        accepted[chosen.canonical_place_id].append(chosen)

    selected: dict[str, tuple[KnowledgeClaimCandidate, ...]] = {}
    for place_id, place_candidates in accepted.items():
        place_candidates.sort(
            key=lambda candidate: (
                CLAIM_TYPE_PRIORITY.get(candidate.claim_type, 99),
                -candidate.source_observed_at.timestamp(),
                candidate.claim_revision_id,
            )
        )
        selected[place_id] = tuple(place_candidates[:max_per_place])
    return selected, tuple(sorted(conflict_groups))


def select_knowledge_candidates(
    candidates: Iterable[KnowledgeClaimCandidate],
    *,
    now: datetime,
    max_per_place: int = 3,
) -> tuple[dict[str, tuple[KnowledgeClaimCandidate, ...]], tuple[tuple[str, str, str], ...]]:
    return _select_knowledge_candidates_cached(
        tuple(candidates),
        now=now,
        max_per_place=max_per_place,
    )


@lru_cache(maxsize=4096)
def _public_view(candidate: KnowledgeClaimCandidate) -> KnowledgeSuggestionView:
    observed = candidate.source_observed_at.date().isoformat()
    valid_until = min(candidate.expires_at, candidate.source_expires_at).date().isoformat()
    return KnowledgeSuggestionView(
        type=candidate.claim_type,
        text=candidate.suggestion_text,
        source_name=candidate.source_name,
        source_url=candidate.source_url,
        freshness=f"更新于 {observed}；有效至 {valid_until}",
    )


def project_knowledge_suggestions(
    result: UserFacingTripResult,
    *,
    canonical_place_by_activity_token: dict[str, str | None],
    candidates: Iterable[KnowledgeClaimCandidate],
    now: datetime,
    max_per_place: int = 3,
) -> KnowledgeProjection:
    selected, conflicts = select_knowledge_candidates(
        candidates,
        now=now,
        max_per_place=max_per_place,
    )
    projected = result.model_copy(deep=True)
    selected_ids: list[str] = []
    for day in projected.days:
        for card in day.activities:
            place_id = canonical_place_by_activity_token.get(card.activity_token)
            chosen = selected.get(place_id or "", ()) if card.status == "READY" else ()
            card.knowledge_suggestions = [_public_view(candidate) for candidate in chosen]
            selected_ids.extend(candidate.claim_revision_id for candidate in chosen)
    return KnowledgeProjection(
        result=projected,
        selected_claim_revision_ids=tuple(sorted(set(selected_ids))),
        conflicted_groups=conflicts,
    )
