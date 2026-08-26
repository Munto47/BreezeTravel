"""POST /api/optimize — 路线优化接口（Phase A v2）

PlannerGraph v2 拓扑：
  clusterer → distance → sequencer → weather_fetcher → scheduler_v2
    → legacy compatibility repair → persist → Audit → conditional tips

响应新增：
  - backup_pool       备选池（因排不下而移出行程的地点）
  - audit_report_id   canonical AuditReport reference for persisted workspaces
  - tips_basis_*      proves Tips were generated after that revision/report
"""

import time
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException

from app.agents.planner import run_planner
from app.memory.working import format_for_prompt
from app.schemas.api import OptimizeRequest, OptimizeResponse
from app.config import get_settings
from app.services.room_access import require_room_member
from app.utils.auth import get_optional_user

router = APIRouter()


@router.post("/optimize", response_model=OptimizeResponse)
async def optimize(request: OptimizeRequest, current_user: str | None = Depends(get_optional_user)):
    cfg = get_settings()
    if request.room_id:
        if current_user is None:
            raise HTTPException(status_code=401, detail="请先登录")
        await require_room_member(request.room_id, current_user, thread_id=request.thread_id)
    elif not cfg.demo_mode:
        raise HTTPException(status_code=400, detail="room_id 必填")
    if not request.places:
        raise HTTPException(status_code=400, detail="places 不能为空")
    if request.task_spec and request.task_spec.needs_clarification:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "TASK_NEEDS_CLARIFICATION",
                "missing_fields": request.task_spec.missing_fields,
                "conflicts": request.task_spec.conflicts,
            },
        )

    start = time.time()

    preferences_text = (
        format_for_prompt(request.working_context) if request.working_context else ""
    )
    persist_canonical = bool(request.persist_workspace or request.workspace_id)

    # D24：解析 GroupPreferences
    from app.schemas.preferences import GroupPreferences
    user_prefs: GroupPreferences | None = None
    if request.user_prefs:
        try:
            user_prefs = GroupPreferences(**request.user_prefs)
        except Exception as e:
            print(f"[Optimize] user_prefs 解析失败（跳过）：{e}")

    try:
        result = await run_planner(
            places=request.places,
            trip_days=request.trip_days,
            thread_id=request.thread_id,
            start_date=request.start_date,
            preferences_text=preferences_text,
            user_prefs=user_prefs,
            vote_counts=request.vote_counts,
            task_spec=request.task_spec,
            planning_input_hash=request.planning_input_hash or "",
            defer_tips=persist_canonical,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "UNSATISFIED_HARD_CONSTRAINT", "message": str(exc)},
        ) from exc

    itinerary = result.itinerary
    backup_pool = result.backup_pool
    violations = result.critic_violations

    total_distance = sum(
        slot.transport.distance_km
        for day in itinerary.days
        for slot in day.slots
        if slot.transport
    )

    workspace_id = None
    itinerary_revision = None
    audit_report_id = None
    audit_status = None
    audit_error_code = None
    tips_status = "LEGACY_INLINE"
    tips_basis_revision = None
    tips_basis_report_id = None
    if persist_canonical:
        tips_status = "BLOCKED_AUDIT_NOT_COMPLETED"
        if current_user is None or request.room_id is None:
            raise HTTPException(
                status_code=401,
                detail={"code": "RESOURCE_SCOPE_DENIED", "message": "持久化行程需要已登录的 room 成员"},
            )
        if itinerary.city not in {"北京", "上海", "杭州"}:
            raise HTTPException(status_code=422, detail={"code": "CITY_NOT_SUPPORTED"})
        if request.trip_days < 2 or request.trip_days > 5:
            raise HTTPException(status_code=422, detail={"code": "TRIP_RANGE_NOT_SUPPORTED"})
        raw_start = request.start_date or next((day.date for day in itinerary.days if day.date), None)
        if raw_start is None:
            raise HTTPException(status_code=422, detail={"code": "TRIP_DATE_REQUIRED"})
        try:
            start_date = date.fromisoformat(str(raw_start))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail={"code": "INVALID_TRIP_DATE"}) from exc

        from app.itineraries.errors import ItineraryDomainError
        from app.itineraries.models import TripDateRange
        from app.itineraries.repositories import PostgresItineraryRepository
        from app.itineraries.revision_service import RevisionService

        repository = PostgresItineraryRepository()
        revision_service = RevisionService(repository)
        try:
            if request.workspace_id:
                workspace = await repository.get_workspace(request.workspace_id)
                if workspace is None:
                    raise HTTPException(status_code=404, detail={"code": "RESOURCE_NOT_FOUND"})
                if workspace.room_id != request.room_id:
                    raise HTTPException(status_code=403, detail={"code": "RESOURCE_SCOPE_DENIED"})
                workspace = await revision_service.attach_initial_legacy_itinerary(
                    workspace=workspace,
                    itinerary=itinerary,
                    created_by=current_user,
                )
            else:
                workspace = await revision_service.create_workspace(
                    room_id=request.room_id,
                    city=itinerary.city,
                    date_range=TripDateRange(
                        start=start_date,
                        end=start_date + timedelta(days=request.trip_days - 1),
                    ),
                    created_by=current_user,
                    initial_legacy_itinerary=itinerary.model_copy(update={"version": 1}),
                )
        except ItineraryDomainError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail()) from exc
        workspace_id = workspace.workspace_id
        itinerary_revision = workspace.current_itinerary_revision
        try:
            from app.audit.evidence_service import EvidenceObservation
            from app.audit.repositories import PostgresAuditRepository
            from app.audit.service import AuditApplicationService
            from app.members.repositories import PostgresMemberConstraintRepository

            observations = []
            input_places = {place.place_id: place for place in request.places}
            seen_place_ids: set[str] = set()
            for day in itinerary.days:
                for slot in day.slots:
                    if slot.place_id in seen_place_ids:
                        continue
                    seen_place_ids.add(slot.place_id)
                    original = input_places.get(slot.place_id)
                    place_payload = (
                        original.model_dump(mode="json") if original is not None else {}
                    )
                    place_payload.update(slot.place or {})
                    provider = (
                        original.retrieval_provider or original.source.value
                        if original is not None
                        else "planner"
                    )
                    observed_at = (
                        original.retrieval_observed_at
                        if original is not None and original.retrieval_observed_at is not None
                        else datetime.now(timezone.utc)
                    )
                    opening_hours = place_payload.get("opening_hours")
                    observations.append(EvidenceObservation(
                        subject_type="PLACE",
                        subject_id=slot.place_id,
                        fact_type="POI_IDENTITY",
                        value=place_payload,
                        provider=provider,
                        observed_at=observed_at,
                        confidence=(
                            1.0
                            if original is not None and original.source.value == "amap_poi"
                            else 0.5
                        ),
                    ))
                    observations.append(EvidenceObservation(
                        subject_type="PLACE",
                        subject_id=slot.place_id,
                        fact_type="OPENING_HOURS",
                        value=opening_hours,
                        provider=provider,
                        observed_at=observed_at,
                        confidence=0.7 if opening_hours else 0,
                        freshness_status=None if opening_hours else "UNAVAILABLE",
                    ))
                if day.weather_summary is not None:
                    observations.append(EvidenceObservation(
                        subject_type="DAY",
                        subject_id=str(day.day_index),
                        fact_type="WEATHER",
                        value=day.weather_summary.model_dump(mode="json"),
                        provider="qweather",
                        observed_at=datetime.now(timezone.utc),
                        confidence=0.8,
                    ))
            audit_report = await AuditApplicationService(
                itinerary_repository=repository,
                audit_repository=PostgresAuditRepository(),
                member_constraint_repository=PostgresMemberConstraintRepository(),
            ).run_current_audit(
                workspace.workspace_id,
                task_id=request.task_spec.task_id if request.task_spec else None,
                extra_observations=observations,
            )
            audit_report_id = audit_report.report_id
            audit_status = "COMPLETED"
            from app.itineraries.errors import TipsNotEligibleError
            from app.itineraries.tips_repositories import PostgresFinalTipsRepository
            from app.itineraries.tips_service import FinalTipsService

            try:
                tips_artifact = await FinalTipsService(
                    itinerary_repository=repository,
                    audit_repository=PostgresAuditRepository(),
                    tips_repository=PostgresFinalTipsRepository(),
                ).generate_for_report(
                    audit_report.report_id,
                    preferences=preferences_text,
                )
                itinerary = tips_artifact.itinerary
                tips_status = "GENERATED"
                tips_basis_revision = tips_artifact.itinerary_revision
                tips_basis_report_id = tips_artifact.report_id
            except TipsNotEligibleError:
                tips_status = "DEFERRED_REPAIR_OR_CONFIRMATION_REQUIRED"
            except Exception:
                # Tips are a derived presentation artifact.  Their failure
                # must not falsify or roll back a completed canonical Audit.
                tips_status = "FAILED_AFTER_AUDIT"
        except Exception as exc:
            # The canonical revision is already durable. Surface audit partial success;
            # never pretend the legacy verifier is the new authoritative report.
            audit_status = "FAILED"
            audit_error_code = getattr(exc, "code", "AUDIT_FAILED")
            tips_status = "BLOCKED_AUDIT_FAILED"

    duration_ms = int((time.time() - start) * 1000)

    return OptimizeResponse(
        itinerary=itinerary,
        total_distance_km=round(total_distance, 1),
        optimization_method="planner_v2",
        duration_ms=duration_ms,
        backup_pool=backup_pool,
        critic_violations=violations,
        task_spec=request.task_spec,
        verification_report=None if persist_canonical else result.verification_report,
        planning_input_hash=(
            request.planning_input_hash
            if persist_canonical
            else result.verification_report.planning_input_hash
            if result.verification_report
            else request.planning_input_hash
        ),
        workspace_id=workspace_id,
        itinerary_revision=itinerary_revision,
        audit_report_id=audit_report_id,
        audit_status=audit_status,
        audit_error_code=audit_error_code,
        tips_status=tips_status,
        tips_basis_revision=tips_basis_revision,
        tips_basis_report_id=tips_basis_report_id,
    )
