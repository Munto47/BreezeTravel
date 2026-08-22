"""Run the isolated synthetic Auditor evaluation lane.

This runner deliberately cannot write the human-calibration manifest.  It uses
the production parser, entity resolver, evidence builder, AuditEngine and
bounded Repair search, but all provider data is deterministic and local.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any


BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.audit.engine import AuditEngine  # noqa: E402
from app.audit.evidence_service import EvidenceObservation, EvidenceService  # noqa: E402
from app.audit.models import AuditRunInput, AuditStatus  # noqa: E402
from app.audit.repositories import InMemoryAuditRepository  # noqa: E402
from app.audit.system_constraints import with_system_constraints  # noqa: E402
from app.importing.entity_resolver import EntityResolver  # noqa: E402
from app.importing.parser import ItineraryTextParser  # noqa: E402
from app.importing.service import parse_time_range  # noqa: E402
from app.itineraries.hash_service import sha256_canonical, with_content_hash  # noqa: E402
from app.itineraries.models import (  # noqa: E402
    ItineraryDay,
    ItineraryRevisionContent,
    ItineraryStop,
    RevisionSource,
    TripDateRange,
    TripWorkspace,
)
from app.itineraries.repositories import InMemoryItineraryRepository  # noqa: E402
from app.repairs.errors import RepairNoFeasibleOptionError  # noqa: E402
from app.repairs.repositories import InMemoryRepairRepository  # noqa: E402
from app.repairs.search import BoundedRepairSearch  # noqa: E402
from app.schemas.task_spec import DateRange, Travelers, TripTaskSpec  # noqa: E402
from scripts.auditor_proxy_contract import (  # noqa: E402
    CALIBRATION_LANE,
    EVIDENCE_TYPE,
    build_role_contracts,
)


EVIDENCE_BOUNDARY = EVIDENCE_TYPE
RUNNER_VERSION = "auditor-simulation-runner-v4"
PIPELINE_CODE_ROOTS = (
    "app/importing",
    "app/audit",
    "app/constraints",
    "app/operations",
    "app/repairs",
    "app/itineraries",
)
PIPELINE_CODE_FILES = ("app/schemas/task_spec.py",)
DEFAULT_DATASET = BACKEND / "eval_data" / "auditor_simulated"
DEFAULT_OUTPUT = BACKEND / "results" / "auditor_simulated" / "latest.json"
SUPPORTED_CITIES = {"北京", "上海", "杭州"}
REFERENCE_TIME = datetime(2026, 8, 20, 8, tzinfo=timezone.utc)


def _canonical_category(value: str) -> str:
    raw = value.strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "time": "time_chain",
        "time_overlap": "time_chain",
        "time_chain_broken": "time_chain",
        "time_data_invalid": "time_chain",
        "schedule_overlap": "time_chain",
        "时间冲突": "time_chain",
        "时间重叠": "time_chain",
        "opening": "opening_hours",
        "closed": "opening_hours",
        "营业时间": "opening_hours",
        "entity": "place_resolution",
        "ambiguous_place": "place_resolution",
        "entity_ambiguous": "place_resolution",
        "entity_not_found": "place_resolution",
        "place_not_resolved": "place_resolution",
        "poi_identity_unknown": "place_resolution",
        "地点歧义": "place_resolution",
        "hotel": "daily_hotel",
        "missing_hotel": "daily_hotel",
        "daily_hotel_missing": "daily_hotel",
        "住宿缺失": "daily_hotel",
        "pace": "system_pacing",
        "pacing": "system_pacing",
        "daily_food_missing": "system_pacing",
        "行程过密": "system_pacing",
        "weather": "weather_exposure",
        "weather_data_missing": "weather_exposure",
        "天气": "weather_exposure",
        "route": "route_edge",
        "travel_time": "route_edge",
        "travel_time_missing": "route_edge",
        "交通": "route_edge",
        "member": "member_constraint",
        "成员约束": "member_constraint",
        "budget": "global_budget",
        "预算": "global_budget",
        "opening_hours_missing": "opening_hours",
        "meal_window_empty": "meal_window_empty",
        "duplicate_place": "duplicate_place",
        "import_parse_failed": "import_parse_failed",
        "import_text_empty": "import_parse_failed",
        "import_text_too_long": "import_parse_failed",
    }
    return aliases.get(raw, raw)


def _finding_category(finding: Any) -> str:
    reason = finding.reason_code.upper()
    rule = finding.rule_id.lower()
    if reason in {"PLACE_NOT_RESOLVED", "POI_IDENTITY_UNKNOWN"}:
        return "place_resolution"
    if "TIME_CHAIN" in reason or "time_chain" in rule:
        return "time_chain"
    if "OPENING" in reason or "opening_hours" in rule:
        return "opening_hours"
    if "HOTEL" in reason or "hotel" in rule:
        return "daily_hotel"
    if "PACING" in reason or "pacing" in rule:
        return "system_pacing"
    if "WEATHER" in reason or "weather" in rule:
        return "weather_exposure"
    if "ROUTE" in reason or "route" in rule or "TRAVEL" in reason:
        return "route_edge"
    if "MEMBER" in reason or "member" in rule:
        return "member_constraint"
    if "BUDGET" in reason or "budget" in rule:
        return "global_budget"
    return _canonical_category(finding.reason_code)


def _expected_categories(case: dict[str, Any]) -> set[str]:
    categories: set[str] = set()
    for item in case.get("simulated_findings", []):
        if not isinstance(item, dict):
            continue
        raw = item.get("reason_code") or item.get("expected_rule_id") or item.get("category") or item.get("risk_category") or item.get("type")
        if raw:
            categories.add(_canonical_category(str(raw)))
    return categories


def _finding_stage(item: dict[str, Any]) -> str:
    reason = str(item.get("reason_code") or "").upper()
    rule = str(item.get("expected_rule_id") or "").lower()
    if reason.startswith("IMPORT_") or rule.startswith("import."):
        return "parser"
    if reason in {"PLACE_NOT_RESOLVED", "POI_IDENTITY_UNKNOWN"} or rule == "audit.input_completeness":
        return "resolution"
    return "audit"


def _item_category(item: dict[str, Any]) -> str | None:
    raw = (
        item.get("reason_code")
        or item.get("expected_rule_id")
        or item.get("category")
        or item.get("risk_category")
        or item.get("type")
    )
    return _canonical_category(str(raw)) if raw else None


def _label_categories_by_stage(case: dict[str, Any], provenance: str) -> dict[str, set[str]]:
    result = {"parser": set(), "resolution": set(), "audit": set()}
    key = "injected_errors" if provenance == "injected" else "original_errors"
    candidates = list(case.get(key, []) or [])
    for item in case.get("simulated_findings", []) or []:
        if not isinstance(item, dict):
            continue
        injected = item.get("injected_by_simulation") is True or item.get("provenance") == "injected_error"
        original = item.get("is_original_error") is True or item.get("provenance") == "original_error"
        if (provenance == "injected" and injected) or (provenance == "original" and original):
            candidates.append(item)
    for item in candidates:
        if isinstance(item, dict):
            category = _item_category(item)
            if category:
                result[_finding_stage(item)].add(category)
        elif item:
            result["audit"].add(_canonical_category(str(item)))
    return result


def _environment_categories_by_stage(case: dict[str, Any]) -> dict[str, set[str]]:
    result = {"parser": set(), "resolution": set(), "audit": set()}
    for item in case.get("simulated_findings", []) or []:
        if not isinstance(item, dict) or item.get("provenance") != "environment_unknown":
            continue
        category = _item_category(item)
        if category:
            result[_finding_stage(item)].add(category)
    return result


def _explicitly_absent_categories(case: dict[str, Any]) -> set[str]:
    values = case.get("not_expected_risk_categories") or case.get("expected_absent_categories") or []
    return {_canonical_category(str(item)) for item in values}


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _pipeline_code_binding(backend_root: Path = BACKEND) -> tuple[str, int]:
    """Bind the production Python pipeline by relative path and exact bytes."""

    files = sorted(
        {
            path
            for relative_root in PIPELINE_CODE_ROOTS
            for path in (backend_root / relative_root).rglob("*.py")
            if path.is_file()
        } | {
            path
            for relative_path in PIPELINE_CODE_FILES
            if (path := backend_root / relative_path).is_file()
        },
        key=lambda path: path.relative_to(backend_root).as_posix(),
    )
    digest = hashlib.sha256()
    for path in files:
        relative = path.relative_to(backend_root).as_posix().encode("utf-8")
        content = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest(), len(files)


def _reject_human_evidence_fields(value: Any, *, path: str) -> None:
    """Fail closed if synthetic input carries a human-evidence contract."""

    forbidden_presence = {
        "human_label",
        "human_labels",
        "human_findings",
        "consent_recorded",
        "human_reviewer",
        "human_feedback",
        "human_organizer",
    }
    if isinstance(value, dict):
        for key, nested in value.items():
            field_path = f"{path}.{key}"
            if key in forbidden_presence:
                raise ValueError(f"synthetic case contains forbidden human evidence field: {field_path}")
            # v1/v2 profiles intentionally assert ``is_real_human: false``.
            # Any other value is evidence-boundary contamination.
            if key == "is_real_human" and nested is not False:
                raise ValueError(f"synthetic case claims real-human provenance: {field_path}")
            _reject_human_evidence_fields(nested, path=field_path)
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _reject_human_evidence_fields(nested, path=f"{path}[{index}]")


class DeterministicFixtureProvider:
    """Offline entity provider whose behavior is scoped to one synthetic case."""

    def __init__(self, city: str, expected: set[str]):
        self.city = city
        self.expected = expected

    async def search(self, *, query: str, city: str) -> list[dict[str, Any]]:
        if "place_resolution" in self.expected and any(
            token in query for token in ("不存在", "同名", "待确认", "未知", "云端秘境")
        ):
            return []
        digest = hashlib.sha256(f"{city}:{query}".encode("utf-8")).hexdigest()
        request_hash = sha256_canonical({
            "provider": "auditor_simulation_fixture",
            "city": city,
            "query": query,
        })
        response_hash = sha256_canonical({"fixture_entity_digest": digest})
        category = "hotel" if any(token in query for token in ("酒店", "宾馆", "民宿", "客栈")) else (
            "food" if any(token in query for token in ("餐厅", "饭店", "小吃", "午餐", "晚餐")) else "attraction"
        )
        return [{
            "place_id": f"sim-{digest[:16]}",
            "name": query,
            "city": city,
            "district": "模拟辖区",
            "address": "synthetic fixture only",
            "category": category,
            "coords": {
                "lng": 116.0 + int(digest[:4], 16) / 65535,
                "lat": 39.0 + int(digest[4:8], 16) / 65535,
            },
            "retrieval_provider": "auditor_simulation_fixture",
            "execution_mode": "fixture",
            "retrieval_request_hash": request_hash,
            "retrieval_response_hash": response_hash,
            "retrieval_observed_at": REFERENCE_TIME.isoformat(),
        }]


def load_dataset(dataset_dir: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    manifest_path = (dataset_dir / "manifest.json").resolve()
    cases_path = (dataset_dir / "cases.jsonl").resolve()
    if not manifest_path.is_file() or not cases_path.is_file():
        raise FileNotFoundError("synthetic dataset requires manifest.json and cases.jsonl")
    manifest_bytes = manifest_path.read_bytes()
    cases_bytes = cases_path.read_bytes()
    manifest = json.loads(manifest_bytes.decode("utf-8"))
    if manifest.get("evidence_boundary") != EVIDENCE_BOUNDARY or manifest.get("human_labels") is not False:
        raise ValueError("dataset is not explicitly isolated from human evidence")
    if manifest.get("cases_file") not in {None, "cases.jsonl"}:
        raise ValueError("manifest cases_file must be cases.jsonl")
    actual_cases_sha256 = _sha256_bytes(cases_bytes)
    if manifest.get("cases_sha256") != actual_cases_sha256:
        raise ValueError("cases.jsonl sha256 does not match manifest")
    cases = [json.loads(line) for line in cases_bytes.decode("utf-8").splitlines() if line.strip()]
    if not cases:
        raise ValueError("synthetic dataset contains no cases")
    if manifest.get("case_count") != len(cases):
        raise ValueError("cases.jsonl case count does not match manifest")
    if len({str(case.get("case_id")) for case in cases}) != len(cases):
        raise ValueError("synthetic case_id values must be unique")
    for index, case in enumerate(cases):
        if case.get("evidence_boundary") != EVIDENCE_BOUNDARY:
            raise ValueError(f"case {index} is not bound to the synthetic evidence boundary")
        if case.get("m1_eligible") is not False:
            raise ValueError(f"case {index} is not explicitly ineligible for M1")
        _reject_human_evidence_fields(case, path=f"cases[{index}]")
    if any(case.get("city") not in SUPPORTED_CITIES for case in cases):
        raise ValueError("synthetic dataset contains a city outside the three-city scope")
    if any(not 2 <= int(case.get("trip_days", 0)) <= 5 for case in cases):
        raise ValueError("synthetic trip_days must be between 2 and 5")
    if any(not 2 <= int(case.get("group_size", 0)) <= 5 for case in cases):
        raise ValueError("synthetic group_size must be between 2 and 5")
    return manifest, cases


def _stage_metrics(
    detected: dict[str, set[str]],
    original: dict[str, set[str]],
    injected: dict[str, set[str]],
    environment: dict[str, set[str]],
) -> dict[str, dict[str, list[str]]]:
    return {
        stage: {
            "detected": sorted(detected[stage]),
            "expected_original": sorted(original[stage]),
            "expected_injected": sorted(injected[stage]),
            "expected_environment_unknown": sorted(environment[stage]),
            "captured_environment_unknown": sorted(
                detected[stage] & environment[stage]
            ),
            "captured_original": sorted(detected[stage] & original[stage]),
            "uncaptured_original": sorted(original[stage] - detected[stage]),
            "additional_unlabelled": sorted(
                detected[stage] - original[stage] - injected[stage] - environment[stage]
            ),
        }
        for stage in ("parser", "resolution", "audit")
    }


def _flatten_stage(metrics: dict[str, dict[str, list[str]]], key: str) -> set[str]:
    return {value for stage in metrics.values() for value in stage[key]}


async def _run_case(case: dict[str, Any]) -> dict[str, Any]:
    case_id = str(case["case_id"])
    city = str(case["city"])
    trip_days = int(case["trip_days"])
    expected = _expected_categories(case)
    parser = ItineraryTextParser()
    draft = parser.parse(str(case["raw_itinerary"]), import_id=f"sim-import:{case_id}")
    original_by_stage = _label_categories_by_stage(case, "original")
    injected_by_stage = _label_categories_by_stage(case, "injected")
    environment_by_stage = _environment_categories_by_stage(case)
    parser_detected = {_canonical_category(error) for error in draft.errors}
    if not draft.raw_stops:
        detected_by_stage = {"parser": parser_detected, "resolution": set(), "audit": set()}
        stage_metrics = _stage_metrics(
            detected_by_stage,
            original_by_stage,
            injected_by_stage,
            environment_by_stage,
        )
        original_expected = set().union(*original_by_stage.values())
        injected_expected = set().union(*injected_by_stage.values())
        return {
            "case_id": case_id,
            "source_document_id": case["source_document_id"],
            "split": case["split"],
            "city": city,
            "source_kind": case["source_kind"],
            "parser": {"stop_count": 0, "errors": draft.errors},
            "entity_resolution": {},
            "diagnostics_by_stage": stage_metrics,
            "audit": {
                "executed": False,
                "skip_reason": "IMPORT_PARSE_FAILED",
                "overall_status": None,
                "risk_categories": [],
                "violated_risk_categories": [],
                "unknown_risk_categories": [],
                "findings": [],
                "evidence_facts": [],
            },
            "repair": {"attempted": False, "proposed": 0, "target_reason_code": None},
            "simulated_repair_decisions": case.get("simulated_repair_decisions", []),
            "expected_original_error_categories": sorted(original_expected),
            "expected_injected_error_categories": sorted(injected_expected),
            "explicitly_absent_risk_categories": sorted(_explicitly_absent_categories(case)),
            "original_error_categories_captured": sorted(
                _flatten_stage(stage_metrics, "captured_original")
            ),
            "original_error_categories_uncaptured": sorted(
                _flatten_stage(stage_metrics, "uncaptured_original")
            ),
            "explicit_false_positive_risk_categories": [],
            "additional_unlabelled_diagnostics": sorted(
                _flatten_stage(stage_metrics, "additional_unlabelled")
            ),
            "honest_unknown_categories": [],
            "boundary_requires_confirmation": True,
        }
    resolver = EntityResolver(DeterministicFixtureProvider(city, expected))
    resolutions = await resolver.resolve_all(draft.raw_stops, city=city)
    resolution_by_stop = {item.raw_stop_id: item for item in resolutions}

    start_date = date(2026, 9, 1)
    workspace_id = f"sim-workspace:{case_id}"
    stops_by_day: dict[int, list[ItineraryStop]] = {index: [] for index in range(trip_days)}
    place_records: dict[str, dict[str, Any]] = {}
    for raw_stop in draft.raw_stops:
        day_index = raw_stop.day_index if raw_stop.day_index is not None else 0
        if day_index not in stops_by_day:
            continue
        resolved = resolution_by_stop[raw_stop.raw_stop_id]
        candidate = resolved.candidates[0] if resolved.candidates else None
        place_id = resolved.canonical_place_id or f"unresolved:{raw_stop.raw_stop_id}"
        start_time, end_time, duration = parse_time_range(raw_stop.raw_time)
        stops_by_day[day_index].append(ItineraryStop(
            stop_id=raw_stop.raw_stop_id,
            place_id=place_id,
            day_index=day_index,
            order_index=len(stops_by_day[day_index]),
            start_time=start_time,
            end_time=end_time,
            visit_duration_minutes=duration,
            raw_name=raw_stop.raw_name,
            source_raw_stop_id=raw_stop.raw_stop_id,
            resolution_status=resolved.resolution_status,
            fixed_commitment=raw_stop.fixed_commitment,
            locked=raw_stop.fixed_commitment,
            commitment_kind=raw_stop.commitment_kind,
            category=candidate.category if candidate and candidate.category else "attraction",
        ))
        if candidate:
            place_records[place_id] = {
                "name": candidate.name,
                "city": city,
                "district": candidate.district,
                "address": candidate.address,
                "category": candidate.category,
                "opening_hours": None if "opening_hours" in expected else "08:00-22:00",
                "provider": "synthetic_fixture",
                "retrieval_observed_at": REFERENCE_TIME,
            }

    date_range = TripDateRange(start=start_date, end=start_date + timedelta(days=trip_days - 1))
    revision = with_content_hash(ItineraryRevisionContent(
        itinerary_id=f"sim-itinerary:{case_id}",
        workspace_id=workspace_id,
        revision=1,
        source_type=RevisionSource.IMPORT,
        city=city,
        date_range=date_range,
        days=[ItineraryDay(day_index=i, date=start_date + timedelta(days=i), stops=stops_by_day[i]) for i in range(trip_days)],
        created_by="synthetic-subagent-simulation",
        created_at=REFERENCE_TIME,
        change_summary={"evidence_boundary": EVIDENCE_BOUNDARY, "case_id": case_id},
    ))
    profile = case.get("simulated_organizer_profile") or {}
    task = with_system_constraints(TripTaskSpec(
        task_id=f"sim-task:{case_id}",
        room_id=f"sim-room:{case_id}",
        city=city,
        date_range=DateRange(start=start_date, days=trip_days),
        travelers=Travelers(adults=int(case["group_size"])),
        assumptions=[f"synthetic organizer profile: {json.dumps(profile, ensure_ascii=False, sort_keys=True)}"],
    ))
    evidence_service = EvidenceService()
    observations = evidence_service.observations_from_revision(
        revision,
        place_records,
        now=REFERENCE_TIME,
    )
    observations.extend(
        EvidenceObservation(
            subject_type="ROUTE_EDGE",
            subject_id=f"{left.stop_id}->{right.stop_id}",
            fact_type="ROUTE_TIME",
            value={"duration_minutes": 30, "mode": "driving"},
            provider="auditor_simulation_fixture",
            observed_at=REFERENCE_TIME,
            confidence=1.0,
        )
        for day in revision.days
        for left, right in zip(day.stops, day.stops[1:])
    )
    snapshot = evidence_service.create_snapshot(
        workspace_id=workspace_id,
        itinerary_revision=1,
        observations=observations,
        now=REFERENCE_TIME,
    )
    report = AuditEngine().run(
        run_input=AuditRunInput(
            workspace_id=workspace_id,
            itinerary_revision=1,
            task_id=task.task_id,
            task_revision=task.task_revision,
            place_resolution_versions={stop.place_id: 1 for day in revision.days for stop in day.stops},
        ),
        revision=revision,
        task_spec=task,
        evidence_snapshot=snapshot,
        now=REFERENCE_TIME,
    )
    detected = {
        _finding_category(item)
        for item in report.findings
        if item.status in {AuditStatus.VIOLATED, AuditStatus.UNKNOWN}
    }
    violated = {
        _finding_category(item) for item in report.findings if item.status == AuditStatus.VIOLATED
    }
    unknown = {
        _finding_category(item) for item in report.findings if item.status == AuditStatus.UNKNOWN
    }

    repair = {"attempted": False, "proposed": 0, "target_reason_code": None}
    repairable_findings = [
        item
        for item in report.findings
        if item.status == AuditStatus.VIOLATED
        and item.repairable
        and item.severity.value in {"BLOCKER", "HIGH"}
    ]
    if repairable_findings:
        repair["attempted"] = True
        repair["target_reason_code"] = repairable_findings[0].reason_code
        itinerary_repo = InMemoryItineraryRepository()
        audit_repo = InMemoryAuditRepository()
        workspace = TripWorkspace(
            workspace_id=workspace_id,
            room_id=task.room_id,
            city=city,
            trip_date_range=date_range,
            current_itinerary_revision=1,
            current_task_spec_revision=1,
            created_by="synthetic-subagent-simulation",
        )
        await itinerary_repo.create_workspace(workspace, revision)
        audit_repo.current_revisions[workspace_id] = 1
        audit_repo.task_specs[workspace_id] = task
        await audit_repo.save_snapshot(snapshot)
        stored_report = await audit_repo.save_report(report)
        itinerary_repo.workspaces[workspace_id] = workspace.model_copy(update={"current_report_id": stored_report.report_id})
        repair_repo = InMemoryRepairRepository(itinerary_repo, audit_repo)
        try:
            options = await BoundedRepairSearch(
                itinerary_repository=itinerary_repo,
                audit_repository=audit_repo,
                repair_repository=repair_repo,
            ).propose(stored_report.report_id, now=REFERENCE_TIME)
            repair["proposed"] = len(options)
            repair["postcheck_report_ids"] = [option.postcheck_report_id for option in options]
        except RepairNoFeasibleOptionError as exc:
            repair["no_feasible_reason"] = exc.code
            repair["no_feasible_detail"] = str(exc)
            if repairable_findings[0].reason_code != "TIME_CHAIN_BROKEN":
                repair["diagnostic_unrepairable_reason"] = (
                    "UNSUPPORTED_BY_BOUNDED_REPAIR_SEARCH:"
                    f"{repairable_findings[0].reason_code}"
                )
            else:
                repair["diagnostic_unrepairable_reason"] = "TIME_CHAIN_CANDIDATES_FAILED_POSTCHECK"

    resolution_detected = (
        {"place_resolution"}
        if any(item.resolution_status.value in {"AMBIGUOUS", "NOT_FOUND"} for item in resolutions)
        else set()
    )
    detected_by_stage = {
        "parser": parser_detected,
        "resolution": resolution_detected,
        # InputCompleteness emits PLACE_NOT_RESOLVED in the AuditReport too,
        # but it is one resolution-stage signal, not a second unlabelled risk.
        "audit": detected - resolution_detected,
    }
    stage_metrics = _stage_metrics(
        detected_by_stage,
        original_by_stage,
        injected_by_stage,
        environment_by_stage,
    )
    original_expected = set().union(*original_by_stage.values())
    injected_expected = set().union(*injected_by_stage.values())
    explicitly_absent = _explicitly_absent_categories(case)
    requires_confirmation = (
        bool(draft.errors)
        or any(item.resolution_status.value in {"AMBIGUOUS", "NOT_FOUND"} for item in resolutions)
        or bool(unknown)
    )

    return {
        "case_id": case_id,
        "source_document_id": case["source_document_id"],
        "split": case["split"],
        "city": city,
        "source_kind": case["source_kind"],
        "parser": {"stop_count": len(draft.raw_stops), "errors": draft.errors},
        "entity_resolution": dict(Counter(item.resolution_status.value for item in resolutions)),
        "diagnostics_by_stage": stage_metrics,
        "audit": {
            "executed": True,
            "overall_status": report.overall_status.value,
            "risk_categories": sorted(detected),
            "violated_risk_categories": sorted(violated),
            "unknown_risk_categories": sorted(unknown),
            "findings": [
                {
                    "finding_id": item.finding_id,
                    "rule_id": item.rule_id,
                    "reason_code": item.reason_code,
                    "status": item.status.value,
                    "severity": item.severity.value,
                    "message": item.message,
                    "evidence_fact_ids": item.evidence_fact_ids,
                    "confirmation_action": item.confirmation_action,
                }
                for item in report.findings if item.status != AuditStatus.SATISFIED
            ],
            "evidence_facts": [
                {
                    "fact_id": fact.fact_id,
                    "subject_type": fact.subject_type,
                    "subject_id": fact.subject_id,
                    "fact_type": fact.fact_type,
                    "value": fact.value,
                    "provider": fact.provider,
                    "response_hash": fact.response_hash,
                    "freshness_status": fact.freshness_status.value,
                }
                for fact in snapshot.facts
            ],
        },
        "repair": repair,
        "simulated_repair_decisions": case.get("simulated_repair_decisions", []),
        "expected_original_error_categories": sorted(original_expected),
        "expected_injected_error_categories": sorted(injected_expected),
        "explicitly_absent_risk_categories": sorted(explicitly_absent),
        "original_error_categories_captured": sorted(
            _flatten_stage(stage_metrics, "captured_original")
        ),
        "original_error_categories_uncaptured": sorted(
            _flatten_stage(stage_metrics, "uncaptured_original")
        ),
        "explicit_false_positive_risk_categories": sorted(detected & explicitly_absent),
        "additional_unlabelled_diagnostics": sorted(
            _flatten_stage(stage_metrics, "additional_unlabelled")
        ),
        "honest_unknown_categories": sorted(unknown),
        "boundary_requires_confirmation": requires_confirmation,
    }


def _aggregate(results: list[dict[str, Any]]) -> dict[str, Any]:
    by_city: dict[str, Counter[str]] = defaultdict(Counter)
    by_kind: dict[str, Counter[str]] = defaultdict(Counter)
    risks: Counter[str] = Counter()
    simulated_decision_count = 0
    simulated_accepted = 0
    simulated_rejected = 0
    simulated_skipped = 0
    simulated_skip_reasons: Counter[str] = Counter()
    simulated_rejection_reasons: Counter[str] = Counter()
    repair_failure_reasons: Counter[str] = Counter()
    diagnostic_unrepairable_reasons: Counter[str] = Counter()
    honest_unknowns: Counter[str] = Counter()
    stage_summary: dict[str, Counter[str]] = {
        stage: Counter() for stage in ("parser", "resolution", "audit")
    }
    for item in results:
        missed = len(item["original_error_categories_uncaptured"])
        false = len(item["explicit_false_positive_risk_categories"])
        additional = len(item["additional_unlabelled_diagnostics"])
        by_city[item["city"]].update(
            cases=1,
            cases_with_uncaptured_original_errors=bool(missed),
            cases_with_explicit_false_positives=bool(false),
            cases_with_additional_unlabelled_diagnostics=bool(additional),
        )
        by_kind[item["source_kind"]].update(
            cases=1,
            cases_with_uncaptured_original_errors=bool(missed),
            cases_with_explicit_false_positives=bool(false),
            cases_with_additional_unlabelled_diagnostics=bool(additional),
        )
        risks.update(item["audit"]["risk_categories"])
        honest_unknowns.update(item["honest_unknown_categories"])
        for stage, metrics in item["diagnostics_by_stage"].items():
            stage_summary[stage].update(
                expected_original=len(metrics["expected_original"]),
                captured_original=len(metrics["captured_original"]),
                uncaptured_original=len(metrics["uncaptured_original"]),
                expected_injected=len(metrics["expected_injected"]),
                expected_environment_unknown=len(metrics["expected_environment_unknown"]),
                captured_environment_unknown=len(metrics["captured_environment_unknown"]),
                additional_unlabelled=len(metrics["additional_unlabelled"]),
            )
        if item["repair"].get("no_feasible_reason"):
            repair_failure_reasons[item["repair"]["no_feasible_reason"]] += 1
        if item["repair"].get("diagnostic_unrepairable_reason"):
            diagnostic_unrepairable_reasons[item["repair"]["diagnostic_unrepairable_reason"]] += 1
        decisions = item.get("simulated_repair_decisions") or []
        if isinstance(decisions, dict):
            decisions = [decisions]
        for decision in decisions:
            if not isinstance(decision, dict):
                continue
            value = str(decision.get("decision") or decision.get("status") or "").strip().lower()
            accepted = decision.get("accepted")
            is_accepted = accepted is True or value in {"accept", "accepted", "apply", "applied", "接受", "采纳"}
            is_rejected = accepted is False or value in {"reject", "rejected", "拒绝", "不采纳"}
            is_skipped = value in {"skip", "skipped", "跳过"}
            if is_skipped:
                simulated_skipped += 1
                reason = str(decision.get("reason_code") or decision.get("reason") or "unspecified")
                simulated_skip_reasons[reason] += 1
                continue
            if not is_accepted and not is_rejected:
                continue
            simulated_decision_count += 1
            if is_accepted:
                simulated_accepted += 1
            else:
                simulated_rejected += 1
                reason = str(
                    decision.get("rejection_reason")
                    or decision.get("reason_code")
                    or decision.get("reason")
                    or "unspecified"
                )
                simulated_rejection_reasons[reason] += 1
    originals_by_document = {
        item["source_document_id"]: item
        for item in results if item["source_kind"] == "SIMULATED_AI_ITINERARY"
    }
    paired_differences = []
    for mutation in (item for item in results if item["source_kind"] == "SIMULATED_CONTROLLED_MUTATION"):
        original = originals_by_document.get(mutation["source_document_id"])
        if original is None:
            paired_differences.append({
                "source_document_id": mutation["source_document_id"],
                "mutation_case_id": mutation["case_id"],
                "pair_status": "ORIGINAL_MISSING",
            })
            continue
        added_by_stage = {
            stage: (
                set(mutation["diagnostics_by_stage"][stage]["detected"])
                - set(original["diagnostics_by_stage"][stage]["detected"])
            )
            for stage in ("parser", "resolution", "audit")
        }
        injected_by_stage = {
            stage: set(mutation["diagnostics_by_stage"][stage]["expected_injected"])
            for stage in ("parser", "resolution", "audit")
        }
        added = set().union(*added_by_stage.values())
        injected = set().union(*injected_by_stage.values())
        explicitly_absent = set(mutation["explicitly_absent_risk_categories"])
        paired_differences.append({
            "source_document_id": mutation["source_document_id"],
            "original_case_id": original["case_id"],
            "mutation_case_id": mutation["case_id"],
            "pair_status": "PAIRED",
            "mutation_added_risk_categories": sorted(added),
            "mutation_added_diagnostics_by_stage": {
                stage: sorted(values) for stage, values in added_by_stage.items()
            },
            "expected_injected_error_categories": sorted(injected),
            "captured_injected_error_categories": sorted(added & injected),
            "uncaptured_injected_error_categories": sorted(injected - added),
            "additional_unlabelled_diagnostics": sorted(added - injected),
            "explicit_false_positive_risk_categories": sorted(added & explicitly_absent),
        })
    paired = [item for item in paired_differences if item["pair_status"] == "PAIRED"]
    injected_expected_count = sum(len(item["expected_injected_error_categories"]) for item in paired)
    injected_captured_count = sum(len(item["captured_injected_error_categories"]) for item in paired)
    boundary = [item for item in results if item["source_kind"] == "SIMULATED_BOUNDARY"]
    return {
        "diagnostic_only": True,
        "quality_gate": False,
        "case_count": len(results),
        "by_city": {key: dict(value) for key, value in sorted(by_city.items())},
        "by_source_kind": {key: dict(value) for key, value in sorted(by_kind.items())},
        "detected_risk_categories": dict(sorted(risks.items())),
        "diagnostics_by_stage": {
            stage: dict(counts) for stage, counts in stage_summary.items()
        },
        "original_errors": {
            "expected_category_count": sum(len(item["expected_original_error_categories"]) for item in results),
            "captured_category_count": sum(len(item["original_error_categories_captured"]) for item in results),
            "uncaptured": [
                {"case_id": item["case_id"], "categories": item["original_error_categories_uncaptured"]}
                for item in results if item["original_error_categories_uncaptured"]
            ],
        },
        "injected_errors_paired_difference": {
            "pair_count": len(paired),
            "missing_original_count": sum(item["pair_status"] != "PAIRED" for item in paired_differences),
            "expected_category_count": injected_expected_count,
            "captured_category_count": injected_captured_count,
            "diagnostic_capture_rate": (
                round(injected_captured_count / injected_expected_count, 4)
                if injected_expected_count else None
            ),
            "pairs": paired_differences,
        },
        "boundary_confirmation": {
            "case_count": len(boundary),
            "requires_confirmation_count": sum(item["boundary_requires_confirmation"] for item in boundary),
            "case_ids_not_requiring_confirmation": [
                item["case_id"] for item in boundary if not item["boundary_requires_confirmation"]
            ],
        },
        "additional_unlabelled_diagnostics": [
            {"case_id": item["case_id"], "categories": item["additional_unlabelled_diagnostics"]}
            for item in results if item["additional_unlabelled_diagnostics"]
        ],
        "honest_unknown": {
            "case_count": sum(bool(item["honest_unknown_categories"]) for item in results),
            "category_counts": dict(sorted(honest_unknowns.items())),
            "cases": [
                {"case_id": item["case_id"], "categories": item["honest_unknown_categories"]}
                for item in results if item["honest_unknown_categories"]
            ],
        },
        "explicit_false_positives": [
            {"case_id": item["case_id"], "categories": item["explicit_false_positive_risk_categories"]}
            for item in results if item["explicit_false_positive_risk_categories"]
        ],
        "repair": {
            "eligible_cases": sum(bool(item["repair"]["attempted"]) for item in results),
            "attempted_cases": sum(bool(item["repair"]["attempted"]) for item in results),
            "cases_with_proposals": sum(item["repair"]["proposed"] > 0 for item in results),
            "proposal_count": sum(item["repair"]["proposed"] for item in results),
            "proposal_case_coverage": (
                round(
                    sum(item["repair"]["proposed"] > 0 for item in results)
                    / sum(bool(item["repair"]["attempted"]) for item in results),
                    4,
                )
                if any(item["repair"]["attempted"] for item in results) else None
            ),
            "no_feasible_reason_distribution": dict(sorted(repair_failure_reasons.items())),
            "diagnostic_unrepairable_reason_distribution": dict(
                sorted(diagnostic_unrepairable_reasons.items())
            ),
        },
        "simulated_repair_decisions_not_human": {
            "decision_count": simulated_decision_count,
            "simulated_accepted_count": simulated_accepted,
            "simulated_rejected_count": simulated_rejected,
            "simulated_skipped_count": simulated_skipped,
            "simulated_acceptance_rate": (
                round(simulated_accepted / simulated_decision_count, 4)
                if simulated_decision_count else None
            ),
            "simulated_rejection_reasons": dict(sorted(simulated_rejection_reasons.items())),
            "simulated_skip_reasons": dict(sorted(simulated_skip_reasons.items())),
            "eligible_for_m1_human_repair_adoption": False,
        },
    }


async def run(dataset_dir: Path, output_path: Path) -> dict[str, Any]:
    manifest, cases = load_dataset(dataset_dir)
    manifest_path = dataset_dir / "manifest.json"
    cases_path = dataset_dir / "cases.jsonl"
    runner_path = Path(__file__).resolve()
    pipeline_code_sha256, pipeline_file_count = _pipeline_code_binding()
    results = []
    for case in cases:
        started = time.perf_counter()
        result = await _run_case(case)
        result["audit_elapsed_seconds"] = round(time.perf_counter() - started, 6)
        results.append(result)
    payload = {
        "schema_version": "2.0",
        "evidence_boundary": EVIDENCE_BOUNDARY,
        "evidence_type": EVIDENCE_TYPE,
        "calibration_lane": CALIBRATION_LANE,
        "human_labels": False,
        "human_validated": False,
        "public_claim_eligible": False,
        "diagnostic_only": True,
        "quality_gate": False,
        "judge": "deterministic_rules_no_external_llm",
        "dataset_manifest_canonical_sha256": sha256_canonical(manifest),
        "manifest_sha256": _sha256_bytes(manifest_path.read_bytes()),
        "cases_sha256": _sha256_bytes(cases_path.read_bytes()),
        "runner_version": RUNNER_VERSION,
        "runner_code_sha256": _sha256_bytes(runner_path.read_bytes()),
        "pipeline_code_sha256": pipeline_code_sha256,
        "pipeline_code_scope": {
            "roots": list(PIPELINE_CODE_ROOTS),
            "files": list(PIPELINE_CODE_FILES),
            "python_file_count": pipeline_file_count,
            "binding": "sorted_relative_path_and_exact_bytes_sha256",
        },
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "deterministic_reference_time": REFERENCE_TIME.isoformat(),
        "proxy_panel": {
            "execution_status": "NOT_RUN",
            "status": "BLOCKED_PROXY_EVALUATORS_NOT_RUN",
            "role_contracts": build_role_contracts()["roles"],
            "human_validated": False,
        },
        "summary": _aggregate(results),
        "cases": results,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-dir", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    report = asyncio.run(run(args.dataset_dir, args.output))
    print(json.dumps(report["summary"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
