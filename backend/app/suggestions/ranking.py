from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.route_priors.loader import RoutePriorLoader
from app.route_priors.models import PriorContribution
from app.schemas.place import PlaceCategory
from app.suggestions.models import (
    EvidenceFreshness,
    FreshnessStatus,
    HardGate,
    RouteDelta,
    SuggestionCandidateDraft,
    SuggestionClassification,
    SuggestionIntent,
)
from app.suggestions.providers import (
    CandidateRouteSource,
    ProviderCandidate,
    ProviderCandidateBatch,
    ProviderCandidateQuery,
    ProviderCandidateSource,
    RouteTimes,
)
from app.suggestions.suitability import classify_provider_suitability


def _hash(value: Any) -> str:
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _normalize(value: str) -> str:
    return "".join(value.casefold().strip().removesuffix("市").split())


class RankingPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = "anchor-ranking-v1"
    on_route_max_minutes: int = Field(default=15, ge=0)
    acceptable_detour_max_minutes: int = Field(default=30, ge=0)
    min_results: int = Field(default=4, ge=1, le=6)
    target_results: int = Field(default=6, ge=4, le=6)
    evidence_max_age_seconds: int = Field(default=86_400, ge=1)
    provider_timeout_seconds: float = Field(default=3.0, gt=0, le=30)
    route_timeout_seconds: float = Field(default=3.0, gt=0, le=30)
    mmr_lambda: float = Field(default=0.82, ge=0, le=1)

    @model_validator(mode="after")
    def validate_thresholds(self) -> "RankingPolicy":
        if self.acceptable_detour_max_minutes < self.on_route_max_minutes:
            raise ValueError("acceptable detour threshold must be at least the on-route threshold")
        if self.target_results < self.min_results:
            raise ValueError("target_results must be at least min_results")
        return self


class RankingContext(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    query: ProviderCandidateQuery
    allowed_categories: frozenset[PlaceCategory] = Field(min_length=1)
    selected_place_ids: frozenset[str] = frozenset()
    selected_place_names: frozenset[str] = frozenset()
    canonical_duplicate_names: frozenset[str] = frozenset()
    as_of: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def validate_as_of(self) -> "RankingContext":
        if self.as_of.tzinfo is None or self.as_of.utcoffset() is None:
            raise ValueError("ranking as_of must be timezone-aware")
        if PlaceCategory.UNKNOWN in self.allowed_categories:
            raise ValueError("UNKNOWN cannot be an allowed suggestion category")
        return self


class RankingResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    policy_version: str
    context_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider_snapshot_id: str | None = None
    provider_status: str = Field(pattern=r"^(OK|EMPTY|TIMEOUT|ERROR)$")
    official_prior_status: str = Field(
        default="NOT_EVALUATED",
        pattern=r"^(NOT_EVALUATED|NOT_CONFIGURED|AVAILABLE|UNAVAILABLE|INTEGRITY_ERROR)$",
    )
    official_prior_reason_code: str | None = None
    candidates: tuple[SuggestionCandidateDraft, ...] = ()
    infeasible_candidates: tuple[SuggestionCandidateDraft, ...] = ()
    excluded_counts: dict[str, int] = Field(default_factory=dict)
    shortage_reason_codes: tuple[str, ...] = ()

    @property
    def acceptable_top3(self) -> tuple[SuggestionCandidateDraft, ...]:
        return tuple(
            candidate
            for candidate in self.candidates
            if candidate.hard_gate.passed
            and candidate.route_delta.status == "AVAILABLE"
            and candidate.evidence_freshness.status is FreshnessStatus.FRESH
            and candidate.classification
            in {SuggestionClassification.ON_ROUTE, SuggestionClassification.ACCEPTABLE_DETOUR}
        )[:3]

    @property
    def visible_candidates(self) -> tuple[SuggestionCandidateDraft, ...]:
        return self.candidates + self.infeasible_candidates


@dataclass(frozen=True)
class _Scored:
    provider: ProviderCandidate
    classification: SuggestionClassification
    hard_gate: HardGate
    route_delta: RouteDelta
    freshness: EvidenceFreshness
    components: dict[str, float]
    total: float
    prior_refs: tuple[str, ...]
    explanation_codes: tuple[str, ...]

    @property
    def acceptable(self) -> bool:
        return (
            self.hard_gate.passed
            and self.route_delta.status == "AVAILABLE"
            and self.freshness.status is FreshnessStatus.FRESH
            and self.classification
            in {SuggestionClassification.ON_ROUTE, SuggestionClassification.ACCEPTABLE_DETOUR}
        )


class AnchorCandidateRanker:
    """Anchor-aware recall, hard filtering, insertion scoring and quota/MMR ranking."""

    def __init__(
        self,
        candidate_source: ProviderCandidateSource,
        route_source: CandidateRouteSource | None,
        *,
        policy: RankingPolicy | None = None,
        route_prior_loader: RoutePriorLoader | None = None,
    ):
        self.candidate_source = candidate_source
        self.route_source = route_source
        self.policy = policy or RankingPolicy()
        self.route_prior_loader = route_prior_loader

    async def rank(self, context: RankingContext) -> RankingResult:
        context_hash = _hash({
            "query": context.query.model_dump(mode="json"),
            "allowed_categories": sorted(category.value for category in context.allowed_categories),
            "selected_place_ids": sorted(context.selected_place_ids),
            "selected_place_names": sorted(context.selected_place_names),
            "canonical_duplicate_names": sorted(context.canonical_duplicate_names),
            "as_of": context.as_of.isoformat(),
            "policy": self.policy.model_dump(mode="json"),
        })
        try:
            batch = await asyncio.wait_for(
                self.candidate_source.search(context.query),
                timeout=self.policy.provider_timeout_seconds,
            )
        except TimeoutError:
            return self._provider_failure(context_hash, "TIMEOUT", "PROVIDER_TIMEOUT")
        except Exception:
            return self._provider_failure(context_hash, "ERROR", "PROVIDER_ERROR")
        if not batch.candidates:
            return RankingResult(
                policy_version=self.policy.version,
                context_hash=context_hash,
                provider_snapshot_id=batch.provider_snapshot_id,
                provider_status="EMPTY",
                shortage_reason_codes=("PROVIDER_EMPTY", "RESULTS_BELOW_MINIMUM"),
            )

        eligible, excluded = self._hard_filter(context, batch)
        if not eligible:
            return RankingResult(
                policy_version=self.policy.version,
                context_hash=context_hash,
                provider_snapshot_id=batch.provider_snapshot_id,
                provider_status="OK",
                excluded_counts=excluded,
                shortage_reason_codes=("NO_CATEGORY_CITY_DEDUP_ELIGIBLE_CANDIDATES", "RESULTS_BELOW_MINIMUM"),
            )

        route_results = await asyncio.gather(*(self._route(context.query, candidate) for candidate in eligible))
        priors, official_status, official_reason = self._prior_signals(context, eligible)
        scored = [
            self._score(context, candidate, route, priors.get(candidate.canonical_place.place_id, {}))
            for candidate, route in zip(eligible, route_results, strict=True)
        ]
        nonblocked = [item for item in scored if item.hard_gate.passed]
        blocked = [item for item in scored if not item.hard_gate.passed]
        selected, quota_codes = self._quota_mmr(nonblocked, context)

        main_drafts = self._drafts(selected[: self.policy.target_results], context_hash, quota_codes, start_rank=1)
        blocked_drafts = self._drafts(
            sorted(blocked, key=self._stable_score_key),
            context_hash,
            {},
            start_rank=len(main_drafts) + 1,
        )
        shortage: list[str] = []
        if len(main_drafts) < self.policy.min_results:
            shortage.append("RESULTS_BELOW_MINIMUM")
        if len(main_drafts) < self.policy.target_results:
            shortage.append("RESULTS_BELOW_TARGET")
        if not any(item.acceptable for item in selected[:3]):
            shortage.append("TOP3_HAS_NO_ACCEPTABLE_CANDIDATE")
        if blocked_drafts:
            shortage.append("HARD_BLOCKED_CANDIDATES_VISIBLE_SEPARATELY")
        return RankingResult(
            policy_version=self.policy.version,
            context_hash=context_hash,
            provider_snapshot_id=batch.provider_snapshot_id,
            provider_status="OK",
            official_prior_status=official_status,
            official_prior_reason_code=official_reason,
            candidates=main_drafts,
            infeasible_candidates=blocked_drafts,
            excluded_counts=excluded,
            shortage_reason_codes=tuple(dict.fromkeys(shortage)),
        )

    def _provider_failure(self, context_hash: str, status: str, reason: str) -> RankingResult:
        return RankingResult(
            policy_version=self.policy.version,
            context_hash=context_hash,
            provider_status=status,
            shortage_reason_codes=(reason, "RESULTS_BELOW_MINIMUM"),
        )

    def _hard_filter(
        self,
        context: RankingContext,
        batch: ProviderCandidateBatch,
    ) -> tuple[list[ProviderCandidate], dict[str, int]]:
        selected_ids = set(context.selected_place_ids)
        selected_names = {_normalize(name) for name in context.selected_place_names | context.canonical_duplicate_names}
        city = _normalize(context.query.city)
        allowed = {category.value for category in context.allowed_categories}
        seen_ids: set[str] = set()
        seen_names: set[str] = set()
        accepted: list[ProviderCandidate] = []
        counts: dict[str, int] = {}
        # This canonical sort intentionally discards Provider ordering before
        # any scoring or quota stage.
        ordered = sorted(
            batch.candidates,
            key=lambda item: (
                item.canonical_place.place_id,
                _normalize(item.canonical_place.name),
                item.provider_receipt.response_hash,
            ),
        )
        for candidate in ordered:
            place = candidate.canonical_place
            name = _normalize(place.name)
            reason: str | None = None
            suitability = classify_provider_suitability(
                name=place.name,
                provider_raw_type=candidate.provider_receipt.provider_raw_type,
                provider_raw_typecode=candidate.provider_receipt.provider_raw_typecode,
            )
            if suitability.category_exclusion_code is not None:
                reason = suitability.category_exclusion_code
            elif _normalize(place.city) != city:
                reason = "WRONG_CITY"
            elif place.category not in allowed:
                reason = "WRONG_CATEGORY"
            elif place.place_id in selected_ids or name in selected_names:
                reason = "ALREADY_SELECTED"
            elif place.place_id in seen_ids or name in seen_names:
                reason = "CANONICAL_DUPLICATE"
            if reason:
                counts[reason] = counts.get(reason, 0) + 1
                continue
            seen_ids.add(place.place_id)
            seen_names.add(name)
            accepted.append(candidate)
        return accepted, counts

    async def _route(self, query: ProviderCandidateQuery, candidate: ProviderCandidate) -> RouteTimes:
        if self.route_source is None:
            return RouteTimes(status="UNKNOWN", reason_code="ROUTE_PROVIDER_NOT_CONFIGURED")
        try:
            return await asyncio.wait_for(
                self.route_source.route_times(query, candidate),
                timeout=self.policy.route_timeout_seconds,
            )
        except TimeoutError:
            return RouteTimes(status="UNKNOWN", reason_code="ROUTE_PROVIDER_TIMEOUT")
        except Exception:
            return RouteTimes(status="UNKNOWN", reason_code="ROUTE_PROVIDER_ERROR")

    def _prior_signals(
        self,
        context: RankingContext,
        candidates: list[ProviderCandidate],
    ) -> tuple[dict[str, dict[str, Any]], str, str | None]:
        if self.route_prior_loader is None:
            return {}, "NOT_CONFIGURED", "ROUTE_PRIOR_LOADER_NOT_CONFIGURED"
        try:
            signals = self.route_prior_loader.signals_for_city(
                context.query.city,
                context.query.anchor_name,
                community_limit=30,
                official_limit=30,
            )
        except Exception:
            return (
                {
                    candidate.canonical_place.place_id: {
                        "status_explanation": "ROUTE_PRIOR_INTEGRITY_UNAVAILABLE",
                    }
                    for candidate in candidates
                },
                "INTEGRITY_ERROR",
                "ROUTE_PRIOR_INTEGRITY_UNAVAILABLE",
            )
        output: dict[str, dict[str, Any]] = {}
        for candidate in candidates:
            candidate_name = _normalize(candidate.canonical_place.name)
            community_matched = [
                hint
                for hint in signals.community_hints
                if _normalize(hint.query_hint) == candidate_name
                or _normalize(hint.query_hint) in candidate_name
                or candidate_name in _normalize(hint.query_hint)
            ]
            official_matched = [
                hint
                for hint in signals.official_hints
                if _normalize(hint.query_hint) == candidate_name
                or _normalize(hint.query_hint) in candidate_name
                or candidate_name in _normalize(hint.query_hint)
            ]
            community_contributions = {
                contribution for hint in community_matched for contribution in hint.contributions
            }
            official_contributions = {
                contribution for hint in official_matched for contribution in hint.contributions
            }
            community_refs = {
                (
                    f"wikivoyage:{hint.source_document_id}@{hint.source_revision}"
                    f"#content-sha256={hint.content_sha256}"
                )
                for hint in community_matched
            }
            official_refs = {
                (
                    f"official-route:{reference.source_document_id}"
                    f"#raw-sha256={reference.raw_sha256}"
                    f"&extract-sha256={reference.extract_sha256}"
                    f"&body-sha256={reference.source_body_sha256}"
                )
                for hint in official_matched
                for reference in hint.official_prior_refs
            }
            item: dict[str, Any] = {
                "community_content": float(PriorContribution.CONTENT_RELEVANCE in community_contributions),
                "community_diversity": float(PriorContribution.DIVERSITY in community_contributions),
                "community_route": float(PriorContribution.ROUTE_ADJACENCY in community_contributions),
                "official_route": float(PriorContribution.ROUTE_ADJACENCY in official_contributions),
                "community_refs": tuple(sorted(community_refs)),
                "official_refs": tuple(sorted(official_refs)),
            }
            if community_matched:
                item["community_explanation"] = "COMMUNITY_PRIOR_NON_FACT"
            if official_matched:
                item["official_explanation"] = "OFFICIAL_ROUTE_PRIOR_HASH_BOUND"
            elif signals.official_status.availability.value == "UNAVAILABLE":
                item["status_explanation"] = "OFFICIAL_ROUTE_PRIOR_UNAVAILABLE"
            output[candidate.canonical_place.place_id] = item
        return (
            output,
            signals.official_status.availability.value,
            signals.official_status.reason_code,
        )

    def _score(
        self,
        context: RankingContext,
        candidate: ProviderCandidate,
        route: RouteTimes,
        priors: dict[str, Any],
    ) -> _Scored:
        freshness = self._freshness(context.as_of, candidate)
        hard_gate = HardGate(
            passed=not candidate.hard_block_codes,
            reason_codes=list(candidate.hard_block_codes),
        )
        route_delta = self._route_delta(route)
        if not hard_gate.passed:
            classification = SuggestionClassification.INFEASIBLE
            components = {"hard_gate": 0.0}
            total = 0.0
        else:
            classification = self._classification(route_delta)
            category_diversity = min(1.0, 0.4 + 0.15 * len(set(candidate.diversity_tags)))
            route_score = self._route_score(route_delta)
            evidence_score = {
                FreshnessStatus.FRESH: 1.0,
                FreshnessStatus.STALE: 0.25,
                FreshnessStatus.UNKNOWN: 0.0,
            }[freshness.status]
            components = {
                "route": route_score,
                "member": candidate.member_suitability,
                "evidence": evidence_score,
                "popularity": candidate.popularity,
                # Official route structure is accepted only from the
                # hash-verifying RoutePriorLoader.  A POI provider resolving
                # identity may not self-assert official provenance.
                "official_route_prior": float(priors.get("official_route", 0.0)),
                # Open-community priors may affect only these three prior
                # buckets; they never alter identity, freshness or popularity.
                "community_content_prior": float(priors.get("community_content", 0.0)),
                "community_diversity_prior": float(priors.get("community_diversity", 0.0)),
                "community_route_prior": float(priors.get("community_route", 0.0)),
                "content_relevance": candidate.content_relevance,
                "diversity": category_diversity,
                "budget": candidate.budget_fit,
                "soft_preference": candidate.soft_preference,
            }
            weights = {
                "route": 0.25,
                "member": 0.12,
                "evidence": 0.12,
                "popularity": 0.10,
                "official_route_prior": 0.08,
                "community_content_prior": 0.03,
                "community_diversity_prior": 0.02,
                "community_route_prior": 0.04,
                "content_relevance": 0.08,
                "diversity": 0.06,
                "budget": 0.05,
                "soft_preference": 0.05,
            }
            total = sum(components[key] * weight for key, weight in weights.items())
        explanations = [classification.value]
        if route_delta.status != "AVAILABLE":
            explanations.extend(("ROUTE_DELTA_UNKNOWN", "REQUIRES_ROUTE_REFRESH"))
        if freshness.status is not FreshnessStatus.FRESH:
            explanations.append(f"EVIDENCE_{freshness.status.value}")
        for key in ("community_explanation", "official_explanation", "status_explanation"):
            if priors.get(key):
                explanations.append(str(priors[key]))
        if candidate.canonical_place.category == PlaceCategory.FOOD.value:
            explanations.append("FOOD_OPTION")
        refs = tuple(dict.fromkeys((
            *priors.get("official_refs", ()),
            *priors.get("community_refs", ()),
        )))
        return _Scored(
            provider=candidate,
            classification=classification,
            hard_gate=hard_gate,
            route_delta=route_delta,
            freshness=freshness,
            components=components,
            total=round(total, 8),
            prior_refs=refs,
            explanation_codes=tuple(dict.fromkeys(explanations)),
        )

    def _freshness(self, as_of: datetime, candidate: ProviderCandidate) -> EvidenceFreshness:
        observed_at = candidate.provider_receipt.observed_at
        age = (as_of - observed_at).total_seconds()
        if age < -300:
            return EvidenceFreshness(
                status=FreshnessStatus.UNKNOWN,
                observed_at=observed_at,
                max_age_seconds=self.policy.evidence_max_age_seconds,
                reason_code="EVIDENCE_OBSERVED_IN_FUTURE",
            )
        if age <= self.policy.evidence_max_age_seconds:
            return EvidenceFreshness(
                status=FreshnessStatus.FRESH,
                observed_at=observed_at,
                max_age_seconds=self.policy.evidence_max_age_seconds,
            )
        return EvidenceFreshness(
            status=FreshnessStatus.STALE,
            observed_at=observed_at,
            max_age_seconds=self.policy.evidence_max_age_seconds,
            reason_code="PROVIDER_RECEIPT_STALE",
        )

    def _route_delta(self, route: RouteTimes) -> RouteDelta:
        if route.status != "AVAILABLE":
            return RouteDelta(status="UNKNOWN", reason_code=route.reason_code or "ROUTE_UNKNOWN")
        if not route.route_receipts:
            return RouteDelta(status="UNKNOWN", reason_code="ROUTE_RECEIPT_MISSING")
        if route.previous_to_candidate_minutes is not None and route.candidate_to_next_minutes is None:
            delta = route.previous_to_candidate_minutes
        elif route.previous_to_candidate_minutes is None:
            assert route.candidate_to_next_minutes is not None
            delta = route.candidate_to_next_minutes
        else:
            assert route.previous_to_next_minutes is not None
            delta = (
                route.previous_to_candidate_minutes
                + route.candidate_to_next_minutes
                - route.previous_to_next_minutes
            )
        return RouteDelta(
            status="AVAILABLE",
            delta_route_minutes=delta,
            previous_to_candidate_minutes=route.previous_to_candidate_minutes,
            candidate_to_next_minutes=route.candidate_to_next_minutes,
            previous_to_next_minutes=route.previous_to_next_minutes,
            route_receipts=route.route_receipts,
        )

    def _classification(self, delta: RouteDelta) -> SuggestionClassification:
        if delta.status != "AVAILABLE" or delta.delta_route_minutes is None:
            return SuggestionClassification.DEFER_TO_OTHER_DAY
        if delta.delta_route_minutes <= self.policy.on_route_max_minutes:
            return SuggestionClassification.ON_ROUTE
        if delta.delta_route_minutes <= self.policy.acceptable_detour_max_minutes:
            return SuggestionClassification.ACCEPTABLE_DETOUR
        return SuggestionClassification.DEFER_TO_OTHER_DAY

    @staticmethod
    def _route_score(delta: RouteDelta) -> float:
        if delta.status != "AVAILABLE" or delta.delta_route_minutes is None:
            return 0.0
        return max(0.0, min(1.0, 1 - max(0, delta.delta_route_minutes) / 60))

    def _quota_mmr(
        self,
        candidates: list[_Scored],
        context: RankingContext,
    ) -> tuple[list[_Scored], dict[str, tuple[str, ...]]]:
        ordered = sorted(candidates, key=self._stable_score_key)
        acceptable = [candidate for candidate in ordered if candidate.acceptable]
        unavailable = [candidate for candidate in ordered if not candidate.acceptable]
        selected: list[_Scored] = []
        quota_codes: dict[str, list[str]] = {}

        def add(item: _Scored | None, code: str) -> None:
            if item is None:
                return
            place_id = item.provider.canonical_place.place_id
            if item not in selected:
                selected.append(item)
            quota_codes.setdefault(place_id, []).append(code)

        add(
            min(
                acceptable,
                key=lambda item: (
                    item.route_delta.delta_route_minutes
                    if item.route_delta.delta_route_minutes is not None
                    else 10**9,
                    item.provider.canonical_place.place_id,
                ),
                default=None,
            ),
            "QUOTA_NEAREST",
        )
        add(
            max(
                acceptable,
                key=lambda item: (item.provider.popularity, -len(item.provider.canonical_place.place_id)),
                default=None,
            ),
            "QUOTA_POPULAR",
        )
        if SuggestionIntent.FOOD in context.query.intents:
            add(
                max(
                    (
                        item
                        for item in acceptable
                        if item.provider.canonical_place.category == PlaceCategory.FOOD.value
                    ),
                    key=lambda item: (item.total, item.provider.canonical_place.place_id),
                    default=None,
                ),
                "QUOTA_FOOD",
            )
        add(
            max(
                acceptable,
                key=lambda item: (
                    len(set(item.provider.diversity_tags)),
                    item.components.get("community_diversity_prior", 0.0),
                    item.total,
                ),
                default=None,
            ),
            "QUOTA_EXPERIENCE_DIVERSITY",
        )

        while len(selected) < self.policy.target_results:
            remaining = [candidate for candidate in acceptable if candidate not in selected]
            if not remaining:
                break
            best = max(
                remaining,
                key=lambda item: (
                    self.policy.mmr_lambda * item.total
                    - (1 - self.policy.mmr_lambda) * self._max_similarity(item, selected),
                    item.total,
                    item.provider.canonical_place.place_id,
                ),
            )
            add(best, "MMR_FILL")
        for item in unavailable:
            if len(selected) >= self.policy.target_results:
                break
            add(item, "VISIBLE_UNAVAILABLE_OR_DEFERRED")
        return selected, {key: tuple(dict.fromkeys(value)) for key, value in quota_codes.items()}

    @staticmethod
    def _max_similarity(candidate: _Scored, selected: list[_Scored]) -> float:
        if not selected:
            return 0.0
        category = candidate.provider.canonical_place.category
        tags = set(candidate.provider.diversity_tags)
        similarities = []
        for other in selected:
            score = 0.55 if other.provider.canonical_place.category == category else 0.0
            other_tags = set(other.provider.diversity_tags)
            if tags or other_tags:
                score += 0.45 * len(tags & other_tags) / max(1, len(tags | other_tags))
            similarities.append(score)
        return max(similarities)

    @staticmethod
    def _stable_score_key(item: _Scored) -> tuple[float, str]:
        return (-item.total, item.provider.canonical_place.place_id)

    @staticmethod
    def _drafts(
        items: list[_Scored],
        context_hash: str,
        quota_codes: dict[str, tuple[str, ...]],
        *,
        start_rank: int,
    ) -> tuple[SuggestionCandidateDraft, ...]:
        output: list[SuggestionCandidateDraft] = []
        for offset, item in enumerate(items):
            place = item.provider.canonical_place
            explanations = (*item.explanation_codes, *quota_codes.get(place.place_id, ()))
            output.append(SuggestionCandidateDraft(
                candidate_id=f"sc-{_hash({'context': context_hash, 'place_id': place.place_id})[:24]}",
                canonical_place=place,
                provider_receipt=item.provider.provider_receipt,
                provider_receipt_id=(
                    f"spr-{item.provider.provider_receipt.response_hash[:16]}-{place.place_id}"
                ),
                rank_position=start_rank + offset,
                classification=item.classification,
                source_prior_refs=list(item.prior_refs),
                score_components=item.components,
                total_score=item.total,
                hard_gate=item.hard_gate,
                route_delta=item.route_delta,
                evidence_freshness=item.freshness,
                explanation_codes=list(dict.fromkeys(explanations)),
                current_facts=item.provider.current_facts,
            ))
        return tuple(output)
