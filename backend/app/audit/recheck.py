"""P8 local pre-trip recheck orchestration and UI-safe diff contracts.

This module deliberately separates the mechanical local recheck from P8's
future human/public-release gates.  A provider adapter may refresh facts, but
any failure is preserved in the new immutable snapshot instead of being hidden
behind the previous successful data.
"""

from __future__ import annotations

from datetime import datetime, time, timezone
from enum import Enum
from typing import Any, Awaitable, Callable, Protocol
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field

from app.audit.evidence_service import EvidenceObservation, EvidenceService
from app.audit.models import (
    AuditFinding,
    AuditReport,
    EvidenceFact,
    EvidenceFreshness,
    EvidenceSnapshot,
    ProviderFailure,
)
from app.audit.repositories import AuditRepository
from app.audit.service import AuditApplicationService
from app.itineraries.errors import ResourceNotFound
from app.itineraries.repositories import ItineraryRepository
from app.operations.models import CreationOperation
from app.operations.repositories import CreationCommandRepository


class EvidenceChangeType(str, Enum):
    ADDED = "ADDED"
    REMOVED = "REMOVED"
    VALUE_CHANGED = "VALUE_CHANGED"
    FRESHNESS_CHANGED = "FRESHNESS_CHANGED"
    VALIDITY_CHANGED = "VALIDITY_CHANGED"
    PROVIDER_CHANGED = "PROVIDER_CHANGED"


class FindingChangeType(str, Enum):
    ADDED = "ADDED"
    RESOLVED = "RESOLVED"
    CHANGED = "CHANGED"


class ProviderFailureChangeType(str, Enum):
    ADDED = "ADDED"
    RESOLVED = "RESOLVED"


class RecheckWindowState(str, Enum):
    """How close this recheck is to the itinerary's first calendar day.

    A workspace stores a trip *date*, not a departure time.  The API therefore
    uses 00:00 Asia/Shanghai on the first trip date as an explicit conservative
    reference point.  It must not imply that a traveller supplied a more
    precise departure timestamp.
    """

    EARLY = "EARLY"
    RECOMMENDED_24_48H = "RECOMMENDED_24_48H"
    LATE = "LATE"


class EvidenceFactChange(BaseModel):
    model_config = ConfigDict(frozen=True)

    change_type: EvidenceChangeType
    subject_type: str
    subject_id: str
    fact_type: str
    provider: str
    before: EvidenceFact | None = None
    after: EvidenceFact | None = None
    reason: str


class AuditFindingChange(BaseModel):
    model_config = ConfigDict(frozen=True)

    change_type: FindingChangeType
    rule_id: str
    reason_code: str
    affected_days: list[int] = Field(default_factory=list)
    affected_stop_ids: list[str] = Field(default_factory=list)
    before: AuditFinding | None = None
    after: AuditFinding | None = None
    reason: str


class ProviderFailureChange(BaseModel):
    model_config = ConfigDict(frozen=True)

    change_type: ProviderFailureChangeType
    failure: ProviderFailure
    reason: str


class PreTripRecheckResult(BaseModel):
    """Append-only local recheck outcome, shaped for a future P8 screen."""

    model_config = ConfigDict(frozen=True)

    source_report_id: str
    source_snapshot_id: str
    report: AuditReport
    evidence_snapshot: EvidenceSnapshot
    evidence_changes: list[EvidenceFactChange] = Field(default_factory=list)
    finding_changes: list[AuditFindingChange] = Field(default_factory=list)
    provider_failure_changes: list[ProviderFailureChange] = Field(default_factory=list)
    provider_failures: list[ProviderFailure] = Field(default_factory=list)
    provider_receipts: list["ProviderRecheckReceipt"] = Field(default_factory=list)
    degraded: bool = False
    recheck_window_state: RecheckWindowState
    trip_start_reference_at: datetime
    hours_until_trip_start: float
    recheck_window_reason: str


class ProviderRecheckReceipt(BaseModel):
    """Truthful, durable-friendly receipt for one P8 evidence-refresh decision.

    ``provider_call_attempted`` is deliberately separate from a Provider name:
    a disabled/misconfigured route can say which provider *would* have been
    used without pretending that a network request occurred.
    """

    provider: str
    provider_call_attempted: bool
    execution_mode: str
    status: str
    subject_id: str | None = None
    query: str | None = None
    response_hash: str | None = None
    observed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    result_count: int | None = None
    detail: str | None = None


class RecheckEvidenceCollection(BaseModel):
    """Refresher output.  Partial success is valid and must remain visible."""

    observations: list[EvidenceObservation] = Field(default_factory=list)
    provider_failures: list[ProviderFailure] = Field(default_factory=list)
    provider_receipts: list[ProviderRecheckReceipt] = Field(default_factory=list)


def _provider_receipt_observation(receipt: ProviderRecheckReceipt) -> EvidenceObservation:
    """Persist a receipt without attributing a non-call to the provider."""
    receipt_source = receipt.provider if receipt.provider_call_attempted else "pre_trip_recheck"
    return EvidenceObservation(
        subject_type="PROVIDER_RECHECK",
        subject_id=receipt.subject_id or "workspace",
        fact_type="POI_REFRESH_RECEIPT",
        value=receipt.model_dump(mode="json"),
        provider=receipt_source,
        observed_at=receipt.observed_at,
        confidence=1.0,
        freshness_status=EvidenceFreshness.FRESH,
    )


class PreTripEvidenceRefresher(Protocol):
    async def collect(
        self,
        *,
        workspace_id: str,
        revision,
        place_records: dict[str, dict],
    ) -> RecheckEvidenceCollection: ...


class StoredEvidenceRefresher:
    """Local baseline refresher.

    It re-materializes the durable POI evidence at recheck time.  Live provider
    adapters can implement ``PreTripEvidenceRefresher`` later without changing
    the command, audit, or UI contracts.  It intentionally makes no claim that
    a provider was contacted.
    """

    def __init__(self, evidence_service: EvidenceService | None = None):
        self.evidence_service = evidence_service or EvidenceService()

    async def collect(self, *, workspace_id: str, revision, place_records: dict[str, dict]) -> RecheckEvidenceCollection:
        return RecheckEvidenceCollection(
            observations=self.evidence_service.observations_from_revision(
                revision,
                place_records,
                now=datetime.now(timezone.utc),
                target_itinerary_revision=revision.revision,
            )
        )


SearchWithAudit = Callable[..., Awaitable[tuple[list[Any], list[dict[str, Any]]]]]


class LiveAmapPoiEvidenceRefresher:
    """Optional live Amap POI refresh with an honest stored-evidence fallback.

    The baseline observations are retained for every place. A successful live
    response replaces only matching POI fields; a timeout or identity mismatch
    leaves baseline evidence in place and records the exact partial failure.
    """

    def __init__(
        self,
        *,
        enabled: bool | None = None,
        stored_refresher: StoredEvidenceRefresher | None = None,
        search_with_audit: SearchWithAudit | None = None,
    ):
        self.enabled = enabled
        self.stored_refresher = stored_refresher or StoredEvidenceRefresher()
        self.search_with_audit = search_with_audit

    def _disabled_reason(self) -> str | None:
        """Return why a real request must not be attempted, otherwise None."""
        from app.config import get_settings

        settings = get_settings()
        enabled = settings.pre_trip_live_provider_recheck_enabled if self.enabled is None else self.enabled
        if not enabled:
            return "live_recheck_disabled"
        # An injected searcher is reserved for deterministic tests/adapters.
        if self.search_with_audit is not None:
            return None
        if settings.runtime_profile not in {"local_real", "public"}:
            return "runtime_profile_forbids_live_provider"
        if settings.demo_mode or settings.amap_mock:
            return "fixture_or_demo_mode_enabled"
        if not settings.amap_api_key:
            return "missing_amap_api_key"
        return None

    @staticmethod
    def _receipt_observation(receipt: ProviderRecheckReceipt) -> EvidenceObservation:
        # A fallback decision is an observation made by this orchestration, not
        # an observation made by Amap.  Keeping it out of ``provider_set``
        # prevents a quick snapshot inspection from falsely inferring a call.
        return _provider_receipt_observation(receipt)

    @staticmethod
    def _audit_value(audits: list[dict[str, Any]]) -> dict[str, Any]:
        return dict(audits[-1]) if audits else {}

    @staticmethod
    def _place_id(place: Any) -> str:
        return str(getattr(place, "place_id", None) or (place.get("place_id") if isinstance(place, dict) else "") or "")

    @staticmethod
    def _place_value(place: Any, field: str, default: Any = None) -> Any:
        if isinstance(place, dict):
            return place.get(field, default)
        return getattr(place, field, default)

    def _live_observations(self, *, place_id: str, place: Any, observed_at: datetime) -> list[EvidenceObservation]:
        coords = self._place_value(place, "coords")
        if hasattr(coords, "model_dump"):
            coords = coords.model_dump(mode="json")
        category = self._place_value(place, "category")
        if hasattr(category, "value"):
            category = category.value
        identity = {
            "place_id": place_id,
            "name": self._place_value(place, "name"),
            "city": self._place_value(place, "city"),
            "district": self._place_value(place, "district"),
            "address": self._place_value(place, "address"),
            "coords": coords,
            "category": category,
        }
        opening_hours = self._place_value(place, "opening_hours")
        observations = [
            EvidenceObservation(
                subject_type="PLACE", subject_id=place_id, fact_type="POI_IDENTITY",
                value=identity, provider="amap_live_recheck", observed_at=observed_at,
            ),
            EvidenceObservation(
                subject_type="PLACE", subject_id=place_id, fact_type="OPENING_HOURS",
                value=opening_hours, provider="amap_live_recheck", observed_at=observed_at,
                confidence=0.7 if opening_hours else 0,
                freshness_status=None if opening_hours else EvidenceFreshness.UNAVAILABLE,
            ),
        ]
        price = self._place_value(place, "amap_price")
        if price is not None:
            observations.append(EvidenceObservation(
                subject_type="PLACE", subject_id=place_id, fact_type="PRICE_REFERENCE",
                value={"amount": price, "currency": "CNY"}, provider="amap_live_recheck",
                observed_at=observed_at, confidence=0.7,
            ))
        return observations

    async def collect(self, *, workspace_id: str, revision, place_records: dict[str, dict]) -> RecheckEvidenceCollection:
        baseline = await self.stored_refresher.collect(
            workspace_id=workspace_id, revision=revision, place_records=place_records,
        )
        reason = self._disabled_reason()
        now = datetime.now(timezone.utc)
        if reason is not None:
            receipt = ProviderRecheckReceipt(
                provider="amap", provider_call_attempted=False, execution_mode="stored_fallback",
                status="not_attempted", observed_at=now, detail=reason,
            )
            return RecheckEvidenceCollection(
                observations=[*baseline.observations, self._receipt_observation(receipt)],
                provider_failures=baseline.provider_failures,
                provider_receipts=[receipt],
            )

        if self.search_with_audit is None:
            from app.tools.amap_tool import _run_amap_search_with_audit
            search_with_audit = _run_amap_search_with_audit
        else:
            search_with_audit = self.search_with_audit
        observations = list(baseline.observations)
        failures = list(baseline.provider_failures)
        receipts: list[ProviderRecheckReceipt] = []
        stops_by_place = {stop.place_id: stop for day in revision.days for stop in day.stops}
        for place_id in sorted(stops_by_place):
            stop = stops_by_place[place_id]
            record = place_records.get(place_id, {})
            query = str(record.get("name") or stop.raw_name or place_id).strip()
            city = str(record.get("city") or revision.city).strip()
            try:
                places, audits = await search_with_audit(query=query, city=city)
                audit = self._audit_value(audits)
                execution_mode = str(audit.get("execution_mode") or "unknown").lower()
                provider = str(audit.get("provider") or "amap")
                observed_at = audit.get("retrieved_at") or now
                if isinstance(observed_at, str):
                    observed_at = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
                response_hash = audit.get("response_hash")
                if execution_mode != "live" or provider != "amap":
                    receipt = ProviderRecheckReceipt(
                        provider=provider, provider_call_attempted=False, execution_mode=execution_mode,
                        status="rejected_non_live_result", subject_id=place_id, query=query,
                        response_hash=response_hash, observed_at=observed_at,
                        result_count=len(places), detail="live_provider_receipt_required",
                    )
                    receipts.append(receipt)
                    failures.append(ProviderFailure(
                        provider="amap", error_category="non_live_provider_result", retryable=False,
                        detail=f"{place_id}: {execution_mode}/{provider}",
                    ))
                    observations.append(self._receipt_observation(receipt))
                    continue
                matched = next((item for item in places if self._place_id(item) == place_id), None)
                if matched is None:
                    receipt = ProviderRecheckReceipt(
                        provider="amap", provider_call_attempted=True, execution_mode="live",
                        status="place_not_returned", subject_id=place_id, query=query,
                        response_hash=response_hash, observed_at=observed_at, result_count=len(places),
                    )
                    receipts.append(receipt)
                    failures.append(ProviderFailure(
                        provider="amap", error_category="selected_place_not_returned", retryable=False,
                        detail=f"{place_id}: exact provider POI id not returned",
                    ))
                    observations.append(self._receipt_observation(receipt))
                    continue
                receipt = ProviderRecheckReceipt(
                    provider="amap", provider_call_attempted=True, execution_mode="live", status="ok",
                    subject_id=place_id, query=query, response_hash=response_hash,
                    observed_at=observed_at, result_count=len(places),
                )
                receipts.append(receipt)
                refreshed_types = {"POI_IDENTITY", "OPENING_HOURS", "PRICE_REFERENCE"}
                observations = [
                    item for item in observations
                    if not (item.subject_type == "PLACE" and item.subject_id == place_id and item.fact_type in refreshed_types)
                ]
                observations.extend(self._live_observations(place_id=place_id, place=matched, observed_at=observed_at))
                observations.append(self._receipt_observation(receipt))
            except Exception as exc:
                audit = getattr(exc, "audit", None)
                audit_value = audit.model_dump(mode="json") if hasattr(audit, "model_dump") else {}
                observed_at = audit_value.get("retrieved_at") or now
                if isinstance(observed_at, str):
                    observed_at = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
                receipt = ProviderRecheckReceipt(
                    provider=str(audit_value.get("provider") or "amap"), provider_call_attempted=True,
                    execution_mode=str(audit_value.get("execution_mode") or "live").lower(),
                    status="error", subject_id=place_id, query=query,
                    response_hash=audit_value.get("response_hash"), observed_at=observed_at,
                    result_count=audit_value.get("result_count"),
                    detail=str(audit_value.get("fallback_reason") or type(exc).__name__),
                )
                receipts.append(receipt)
                failures.append(ProviderFailure(
                    provider="amap", error_category=str(audit_value.get("fallback_reason") or "provider_exception"),
                    retryable=True, detail=f"{place_id}: {type(exc).__name__}",
                ))
                observations.append(self._receipt_observation(receipt))
        return RecheckEvidenceCollection(
            observations=observations, provider_failures=failures, provider_receipts=receipts,
        )


def _fact_key(fact: EvidenceFact) -> tuple[str, str, str, str]:
    return (fact.subject_type, fact.subject_id, fact.fact_type, fact.provider)


def _fact_subject_key(fact: EvidenceFact) -> tuple[str, str, str]:
    return (fact.subject_type, fact.subject_id, fact.fact_type)


def _finding_key(finding: AuditFinding) -> tuple:
    return (
        finding.rule_id,
        finding.reason_code,
        tuple(finding.affected_days),
        tuple(finding.affected_stop_ids),
        tuple(finding.affected_member_ids),
    )


def _failure_key(failure: ProviderFailure) -> tuple[str, str, bool, str | None]:
    return (failure.provider, failure.error_category, failure.retryable, failure.detail)


def _same_provider_change(old: EvidenceFact, new: EvidenceFact) -> EvidenceFactChange | None:
    """Return a change for a uniquely matched provider fact, if there is one."""

    common = {
        "subject_type": old.subject_type,
        "subject_id": old.subject_id,
        "fact_type": old.fact_type,
        "provider": old.provider,
        "before": old,
        "after": new,
    }
    if old.value != new.value:
        return EvidenceFactChange(
            change_type=EvidenceChangeType.VALUE_CHANGED,
            reason="同一来源返回的事实值发生变化。",
            **common,
        )
    if old.freshness_status != new.freshness_status:
        return EvidenceFactChange(
            change_type=EvidenceChangeType.FRESHNESS_CHANGED,
            reason="证据的新鲜度按当前 Evidence Policy 重新判定。",
            **common,
        )
    if (old.valid_from, old.valid_until) != (new.valid_from, new.valid_until):
        return EvidenceFactChange(
            change_type=EvidenceChangeType.VALIDITY_CHANGED,
            reason="复检刷新了证据的有效时间窗口。",
            **common,
        )
    return None


def _added_change(fact: EvidenceFact) -> EvidenceFactChange:
    return EvidenceFactChange(
        change_type=EvidenceChangeType.ADDED,
        subject_type=fact.subject_type,
        subject_id=fact.subject_id,
        fact_type=fact.fact_type,
        provider=fact.provider,
        after=fact,
        reason="复检新增了这项证据事实。",
    )


def _removed_change(fact: EvidenceFact) -> EvidenceFactChange:
    return EvidenceFactChange(
        change_type=EvidenceChangeType.REMOVED,
        subject_type=fact.subject_type,
        subject_id=fact.subject_id,
        fact_type=fact.fact_type,
        provider=fact.provider,
        before=fact,
        reason="复检结果不再包含这项证据事实。",
    )


def diff_evidence_snapshots(before: EvidenceSnapshot, after: EvidenceSnapshot) -> list[EvidenceFactChange]:
    """Diff evidence without inventing a provider replacement relation.

    A ``PROVIDER_CHANGED`` result is useful only when one old fact and one new
    fact have the identical subject/fact identity.  Multiple candidates make
    the relationship unknowable, so those facts deliberately remain explicit
    add/remove entries rather than being paired by a heuristic.
    """

    before_by_subject: dict[tuple[str, str, str], list[EvidenceFact]] = {}
    after_by_subject: dict[tuple[str, str, str], list[EvidenceFact]] = {}
    for fact in before.facts:
        before_by_subject.setdefault(_fact_subject_key(fact), []).append(fact)
    for fact in after.facts:
        after_by_subject.setdefault(_fact_subject_key(fact), []).append(fact)

    changes: list[EvidenceFactChange] = []
    for subject_key in sorted(set(before_by_subject) | set(after_by_subject)):
        old_remaining = sorted(before_by_subject.get(subject_key, []), key=lambda item: (item.provider, item.fact_id))
        new_remaining = sorted(after_by_subject.get(subject_key, []), key=lambda item: (item.provider, item.fact_id))

        # First match exact providers.  A duplicate under the same provider is
        # ambiguous too, so it stays in the explicit added/removed lane.
        old_by_provider: dict[str, list[EvidenceFact]] = {}
        new_by_provider: dict[str, list[EvidenceFact]] = {}
        for fact in old_remaining:
            old_by_provider.setdefault(fact.provider, []).append(fact)
        for fact in new_remaining:
            new_by_provider.setdefault(fact.provider, []).append(fact)
        matched_old_ids: set[str] = set()
        matched_new_ids: set[str] = set()
        for provider in sorted(set(old_by_provider) & set(new_by_provider)):
            old_candidates, new_candidates = old_by_provider[provider], new_by_provider[provider]
            if len(old_candidates) == len(new_candidates) == 1:
                old, new = old_candidates[0], new_candidates[0]
                matched_old_ids.add(old.fact_id)
                matched_new_ids.add(new.fact_id)
                change = _same_provider_change(old, new)
                if change is not None:
                    changes.append(change)

        old_remaining = [fact for fact in old_remaining if fact.fact_id not in matched_old_ids]
        new_remaining = [fact for fact in new_remaining if fact.fact_id not in matched_new_ids]

        # Only an exact 1:1 remainder is a defensible source swap.  The
        # after-provider is displayed as the current provider; before/after
        # carry both durable facts for the client.
        if (
            len(old_remaining) == len(new_remaining) == 1
            and old_remaining[0].provider != new_remaining[0].provider
        ):
            old, new = old_remaining[0], new_remaining[0]
            changes.append(EvidenceFactChange(
                change_type=EvidenceChangeType.PROVIDER_CHANGED,
                subject_type=new.subject_type,
                subject_id=new.subject_id,
                fact_type=new.fact_type,
                provider=new.provider,
                before=old,
                after=new,
                reason=f"同一主体和事实类型的唯一证据来源由 {old.provider} 切换为 {new.provider}。",
            ))
            continue
        changes.extend(_removed_change(fact) for fact in old_remaining)
        changes.extend(_added_change(fact) for fact in new_remaining)
    return changes


def diff_audit_reports(before: AuditReport, after: AuditReport) -> list[AuditFindingChange]:
    before_by_key = {_finding_key(finding): finding for finding in before.findings}
    after_by_key = {_finding_key(finding): finding for finding in after.findings}
    changes: list[AuditFindingChange] = []
    for key in sorted(set(before_by_key) | set(after_by_key)):
        old, new = before_by_key.get(key), after_by_key.get(key)
        rule_id, reason_code, days, stops, _ = key
        common = {"rule_id": rule_id, "reason_code": reason_code, "affected_days": list(days), "affected_stop_ids": list(stops)}
        if old is None:
            changes.append(AuditFindingChange(
                change_type=FindingChangeType.ADDED, after=new, reason="新的证据或规则输入触发了该审计结论。", **common
            ))
        elif new is None:
            changes.append(AuditFindingChange(
                change_type=FindingChangeType.RESOLVED, before=old, reason="复检后该审计结论不再成立。", **common
            ))
        elif (old.status, old.severity, old.message, old.input_values) != (new.status, new.severity, new.message, new.input_values):
            changes.append(AuditFindingChange(
                change_type=FindingChangeType.CHANGED, before=old, after=new,
                reason="相同审计项的状态、严重度或输入事实发生变化。", **common
            ))
    return changes


def diff_provider_failures(before: EvidenceSnapshot, after: EvidenceSnapshot) -> list[ProviderFailureChange]:
    old = {_failure_key(item): item for item in before.provider_failures}
    new = {_failure_key(item): item for item in after.provider_failures}
    changes: list[ProviderFailureChange] = []
    for key in sorted(set(old) | set(new)):
        if key not in old:
            changes.append(ProviderFailureChange(
                change_type=ProviderFailureChangeType.ADDED, failure=new[key],
                reason="复检期间该 Provider 未能完成相应事实的刷新。",
            ))
        elif key not in new:
            changes.append(ProviderFailureChange(
                change_type=ProviderFailureChangeType.RESOLVED, failure=old[key],
                reason="本次复检未再出现该 Provider 失败。",
            ))
    return changes


class PreTripRecheckService:
    def __init__(
        self,
        *,
        itinerary_repository: ItineraryRepository,
        audit_repository: AuditRepository,
        audit_service: AuditApplicationService | None = None,
        evidence_refresher: PreTripEvidenceRefresher | None = None,
    ):
        self.itinerary_repository = itinerary_repository
        self.audit_repository = audit_repository
        self.audit_service = audit_service or AuditApplicationService(
            itinerary_repository=itinerary_repository,
            audit_repository=audit_repository,
        )
        self.evidence_refresher = evidence_refresher or LiveAmapPoiEvidenceRefresher()

    @staticmethod
    def _window_for_trip_start(*, trip_start, reference_at: datetime) -> tuple[RecheckWindowState, datetime, float, str]:
        """Classify the recheck against the stored trip-date boundary.

        No departure time is stored in ``TripDateRange``.  We intentionally use
        the start of the first travel date in the product's China time zone and
        expose that reference in the result, so neither API nor UI overstates
        the precision of the user's itinerary.
        """

        china_tz = ZoneInfo("Asia/Shanghai")
        reference_at = reference_at if reference_at.tzinfo else reference_at.replace(tzinfo=timezone.utc)
        reference_at = reference_at.astimezone(china_tz)
        trip_start_reference_at = datetime.combine(trip_start, time.min, tzinfo=china_tz)
        hours_until = round((trip_start_reference_at - reference_at).total_seconds() / 3600, 2)
        if hours_until > 48:
            return (
                RecheckWindowState.EARLY,
                trip_start_reference_at,
                hours_until,
                "距行程首日零点仍超过 48 小时；可以复检，但尚未进入建议的 24～48 小时窗口。",
            )
        if hours_until >= 24:
            return (
                RecheckWindowState.RECOMMENDED_24_48H,
                trip_start_reference_at,
                hours_until,
                "当前位于以行程首日零点计算的出发前 24～48 小时复检窗口。",
            )
        return (
            RecheckWindowState.LATE,
            trip_start_reference_at,
            hours_until,
            "距行程首日零点不足 24 小时或行程已开始；复检仍会执行，但应尽快核对变化。",
        )

    async def read_persisted_result(self, *, recheck_report_id: str) -> PreTripRecheckResult:
        """Rebuild one completed recheck from its immutable report/snapshot chain."""
        report = await self.audit_repository.get_report(recheck_report_id)
        if report is None:
            raise ResourceNotFound("recheck audit report does not exist", context={"report_id": recheck_report_id})
        if report.supersedes_report_id is None:
            raise ResourceNotFound("audit report is not a pre-trip recheck", context={"report_id": recheck_report_id})
        source_report = await self.audit_repository.get_report(report.supersedes_report_id)
        snapshot = await self.audit_repository.get_snapshot(report.evidence_snapshot_id)
        if source_report is None or snapshot is None:
            raise ResourceNotFound("recheck lineage is incomplete", context={"report_id": recheck_report_id})
        if snapshot.supersedes_snapshot_id != source_report.evidence_snapshot_id:
            raise ResourceNotFound("audit report is not a pre-trip recheck", context={"report_id": recheck_report_id})
        source_snapshot = await self.audit_repository.get_snapshot(source_report.evidence_snapshot_id)
        if source_snapshot is None:
            raise ResourceNotFound("source evidence snapshot does not exist", context={"snapshot_id": source_report.evidence_snapshot_id})
        provider_receipts = [
            ProviderRecheckReceipt.model_validate(fact.value)
            for fact in snapshot.facts
            if fact.subject_type == "PROVIDER_RECHECK"
            and fact.fact_type == "POI_REFRESH_RECEIPT"
            and isinstance(fact.value, dict)
        ]
        # Supersession alone is not enough: ordinary audit and route refreshes
        # also append reports.  This receipt is written only by P8 recheck.
        if not provider_receipts:
            raise ResourceNotFound("audit report is not a pre-trip recheck", context={"report_id": recheck_report_id})
        revision = await self.itinerary_repository.get_revision(report.workspace_id, report.itinerary_revision)
        if revision is None:
            raise ResourceNotFound(
                "recheck itinerary revision does not exist",
                context={"workspace_id": report.workspace_id, "revision": report.itinerary_revision},
            )
        window_state, trip_start_reference_at, hours_until_trip_start, window_reason = self._window_for_trip_start(
            trip_start=revision.date_range.start,
            reference_at=snapshot.created_at,
        )
        return PreTripRecheckResult(
            source_report_id=source_report.report_id,
            source_snapshot_id=source_snapshot.snapshot_id,
            report=report,
            evidence_snapshot=snapshot,
            evidence_changes=diff_evidence_snapshots(source_snapshot, snapshot),
            finding_changes=diff_audit_reports(source_report, report),
            provider_failure_changes=diff_provider_failures(source_snapshot, snapshot),
            provider_failures=snapshot.provider_failures,
            provider_receipts=provider_receipts,
            degraded=bool(snapshot.provider_failures),
            recheck_window_state=window_state,
            trip_start_reference_at=trip_start_reference_at,
            hours_until_trip_start=hours_until_trip_start,
            recheck_window_reason=window_reason,
        )

    async def run_idempotent(
        self,
        *,
        source_report_id: str,
        actor_user_id: str,
        idempotency_key: str,
        command_repository: CreationCommandRepository,
        now: datetime | None = None,
    ) -> tuple[PreTripRecheckResult, bool]:
        source_report = await self.audit_repository.get_report(source_report_id)
        if source_report is None:
            raise ResourceNotFound("source audit report does not exist", context={"report_id": source_report_id})
        source_snapshot = await self.audit_repository.get_snapshot(source_report.evidence_snapshot_id)
        if source_snapshot is None:
            raise ResourceNotFound("source evidence snapshot does not exist", context={"snapshot_id": source_report.evidence_snapshot_id})
        workspace = await self.itinerary_repository.get_workspace(source_report.workspace_id)
        if workspace is None or workspace.current_itinerary_revision is None:
            raise ResourceNotFound("workspace does not have a current itinerary revision")
        revision = await self.itinerary_repository.get_revision(source_report.workspace_id, workspace.current_itinerary_revision)
        if revision is None:
            raise ResourceNotFound("current itinerary revision does not exist")
        place_ids = sorted({stop.place_id for day in revision.days for stop in day.stops})
        place_records = await self.audit_repository.load_place_records(
            source_report.workspace_id,
            place_ids,
            target_itinerary_revision=revision.revision,
        )
        command_now = now or datetime.now(timezone.utc)
        collection = await self.evidence_refresher.collect(
            workspace_id=source_report.workspace_id,
            revision=revision,
            place_records=place_records,
        )
        if not collection.provider_receipts:
            # Custom/local adapters may only return observations or failures.
            # Record that limitation explicitly so this bundle stays
            # identifiable and never implies an unrecorded provider call.
            receipt = ProviderRecheckReceipt(
                provider="unknown",
                provider_call_attempted=False,
                execution_mode="adapter_without_receipt",
                status="not_reported",
                observed_at=command_now,
                detail="refresher_did_not_report_provider_receipt",
            )
            collection = collection.model_copy(update={
                "observations": [*collection.observations, _provider_receipt_observation(receipt)],
                "provider_receipts": [receipt],
            })
        report, replayed = await self.audit_service.run_current_audit_idempotent(
            source_report.workspace_id,
            operation=CreationOperation.PRE_TRIP_RECHECK,
            target_id=source_report_id,
            actor_user_id=actor_user_id,
            idempotency_key=idempotency_key,
            request_body={"source_report_id": source_report_id},
            command_repository=command_repository,
            task_id=source_report.task_id,
            provider_failures=collection.provider_failures,
            evidence_observations=collection.observations,
            now=command_now,
        )
        # Reconstruct from durable records so an idempotent replay cannot
        # leak a fresh collector timestamp into the original response.
        return await self.read_persisted_result(recheck_report_id=report.report_id), replayed
