"""Source-requested hotel changes constrain identity, never a whole brand."""
from datetime import datetime, timezone

import pytest

from app.trip_understanding.map_repository import plan_with_stay_anchor
from app.trip_understanding.candidates import CandidatePlace, GCJ02Position, issue_candidate
from app.trip_understanding.errors import ResourceNotReadyError
from app.trip_understanding.models import PlaceConfirmCommand, StayCandidateView, UndoCommand
from app.trip_understanding.overnight_context import overnight_segments, stay_context_hash
from app.trip_understanding.pipeline import TripUnderstandingPipeline, canonical_sha256
from app.trip_understanding.service import TripUnderstandingApplicationService
from app.trip_understanding.stay import ControlledStayRouteProvider, StayRecommendationEngine, stay_plan_from_map
from app.trip_understanding.stay_repository import _current_selections, _has_selected_night, _overnight_metadata, _segmented_view, _selection_binding
from tests.test_g02_map_stay import _map_plan, _test_registry
from tests.test_experience_text_fidelity import DraftProvider, RecordingResolver, activity
from tests.test_experience_v3_journey import repository_for, refresh
from tests.test_stay_manual_refresh import finish_stay, job_count
from tests.test_stay_overnight_segments import CityHotels


def checkout_plan(*, excluded=(1,), confirmed=True):
    base = _map_plan()
    hotel = base.stops[0].model_copy(update={"name": "汉庭酒店(合成0店)", "category": "住宿",
        "lodging_event": "CHECK_OUT", "lodging_excluded_nights": list(excluded),
        "canonical_place_id": "amap:北京-0" if confirmed else None,
        "resolution_status": "AUTO_MATCHED" if confirmed else "NEEDS_CONFIRMATION"})
    return base.model_copy(update={"stops": [hotel, *[s.model_copy(update={"sequence_index": s.sequence_index+1})
        if s.day_index == 1 else s for s in base.stops]]})


@pytest.mark.asyncio
async def test_explicit_other_hotel_excludes_same_identity_before_recall_but_keeps_other_branch():
    plan = checkout_plan()
    segment = overnight_segments(plan)[0]
    assert segment.overnight_days == [1, 2] and segment.excluded_place_ids == ["北京-0"]
    result = await StayRecommendationEngine(CityHotels(), ControlledStayRouteProvider(), brand_registry=_test_registry()).recommend(
        stay_plan_from_map(plan), observed_at=datetime.now(timezone.utc))
    assert result.candidates and all(c.candidate.canonical_place_id != "北京-0" for c in result.candidates)
    assert any(c.candidate.brand == "汉庭" for c in result.candidates)
    assert "已按原文另住要求排除原门店" in result.provider_binding["segments"][0]["message"]
    assert not any(s.is_stay_anchor for s in plan_with_stay_anchor(plan, selected_place_id="北京-0", selected_name="原店",
        selected_city="北京", longitude=116.4, latitude=39.9, overnight_days=[1, 2]).stops)
    assert any(s.is_stay_anchor for s in plan_with_stay_anchor(plan, selected_place_id="北京-1", selected_name="另一分店",
        selected_city="北京", longitude=116.4, latitude=39.9, overnight_days=[1, 2]).stops)


@pytest.mark.asyncio
async def test_checkout_alone_does_not_exclude_the_old_hotel():
    plan = checkout_plan(excluded=())
    assert overnight_segments(plan)[0].excluded_place_ids == []
    result = await StayRecommendationEngine(CityHotels(), ControlledStayRouteProvider(), brand_registry=_test_registry()).recommend(
        stay_plan_from_map(plan), observed_at=datetime.now(timezone.utc))
    assert "北京-0" in {c.candidate.canonical_place_id for c in result.candidates}
    assert stay_context_hash(plan) != stay_context_hash(checkout_plan())


@pytest.mark.asyncio
async def test_unconfirmed_old_branch_marks_only_relevant_night_pending_instead_of_excluding_same_brand():
    plan = checkout_plan(confirmed=False)
    first, second = overnight_segments(plan)
    assert first.overnight_days == [1] and first.uncertain and first.excluded_place_ids == []
    assert first.unconfirmed_exclusions == ["汉庭酒店(合成0店)"]
    assert second.overnight_days == [2] and not second.uncertain
    view = _segmented_view(_overnight_metadata(plan), [])
    assert view.segments[0].status == "LIMITED" and "具体门店尚未核实" in view.segments[0].message
    result = await StayRecommendationEngine(CityHotels(), ControlledStayRouteProvider(), brand_registry=_test_registry()).recommend(
        stay_plan_from_map(plan), observed_at=datetime.now(timezone.utc))
    assert result.candidates and all(c.candidate.provider_binding["overnight_days"] == [2] for c in result.candidates)


def test_night_specific_exclusion_does_not_replace_a_different_explicit_booking_or_earlier_return():
    plan = checkout_plan(excluded=(2,))
    source = plan.stops[0].model_copy(update={"lodging_event": "OVERNIGHT", "lodging_scope": "DAY",
        "lodging_excluded_nights": [], "sequence_index": 9})
    plan = plan.model_copy(update={"stops": [*plan.stops, source]})
    first, second = overnight_segments(plan)
    assert first.overnight_days == [1] and first.preserved_hotels == [source.name] and first.excluded_place_ids == []
    assert second.overnight_days == [2] and not second.preserved_hotels and second.excluded_place_ids == ["北京-0"]


def test_whole_trip_hotel_constraint_can_exclude_one_night_without_erasing_other_booked_nights():
    base = _map_plan()
    source = base.stops[0].model_copy(update={"name": "原酒店", "category": "住宿", "lodging_event": "OVERNIGHT",
        "lodging_scope": "WHOLE_TRIP", "lodging_excluded_nights": [2], "canonical_place_id": "source-hotel"})
    first, second = overnight_segments(base.model_copy(update={"lodging_constraints": [source]}))
    assert first.preserved_hotels == [source.name] and first.overnight_days == [1]
    assert second.preserved_hotels == [] and second.overnight_days == [2] and second.excluded_place_ids == ["source-hotel"]


def test_next_night_explicit_return_to_original_hotel_remains_a_separate_booking():
    plan = checkout_plan(excluded=(1,))
    returned = plan.stops[0].model_copy(update={"day_index": 2, "day_label": "Day 2", "sequence_index": 9,
        "lodging_event": "OVERNIGHT", "lodging_scope": "DAY", "lodging_excluded_nights": []})
    first, second = overnight_segments(plan.model_copy(update={"stops": [*plan.stops, returned]}))
    assert first.overnight_days == [1] and first.excluded_place_ids == ["北京-0"] and not first.preserved_hotels
    assert second.overnight_days == [2] and not second.excluded_place_ids and second.preserved_hotels == [returned.name]


@pytest.mark.parametrize("kind", ["memory", "postgres"])
@pytest.mark.asyncio
async def test_source_exclusion_persists_and_api_rejects_an_upstream_candidate_for_the_same_branch(kind):
    clause = "上午从北京饭店退房，然后游览故宫博物院，今晚另找一家酒店"
    source = f"北京三日游。Day1：{clause}。Day2：天坛公园。Day3：颐和园。"
    rows = [{**activity("北京饭店"), "category": "住宿", "lodging_event": "CHECK_OUT",
        "lodging_evidence": "上午从北京饭店退房", "lodging_excluded_nights": [1], "lodging_exclusion_evidence": clause},
        activity("故宫博物院"), activity("天坛公园", 2), activity("颐和园", 3)]
    output = await TripUnderstandingPipeline(DraftProvider(rows), RecordingResolver()).run(source)
    assert output.public_result.days[0].activities[0].lodging_excluded_nights == [1]
    async with repository_for(kind) as repo:
        now = datetime.now(timezone.utc)
        created = await repo.create_demo(capability_hash="a"*64, source_text=source, idempotency_key="excluded-source-hotel",
            request_hash=canonical_sha256({"text": source}), now=now, ttl_hours=24)
        job = await repo.claim_next(worker_id="exclusion-source", now=now, lease_seconds=60)
        await repo.complete_job(job, output, now=now)
        resource = await repo.authorize(created.accepted.public_resource_id, capability_hash="a"*64, now=now)
        current = await repo.get_result(resource)
        map_plan, _ = await repo.get_current_place_plan(resource)
        original = next(s for s in map_plan.stops if s.name == "北京饭店")
        assert original.lodging_excluded_nights == [1]
        stay_job = await repo.claim_next_stay(worker_id="exclusion-stay", now=now, lease_seconds=60)
        plan = await repo.load_stay_plan(stay_job)
        result = await StayRecommendationEngine(CityHotels(), ControlledStayRouteProvider(), brand_registry=_test_registry()).recommend(
            plan, observed_at=now)
        # Simulate a stale/incorrect upstream snapshot with current version and
        # otherwise valid identity metadata. Selection must recheck the source.
        forged = result.candidates[0].model_copy(deep=True)
        forged.candidate.canonical_place_id = original.canonical_place_id
        await repo.complete_stay_job(stay_job, result.model_copy(update={"candidates": [forged]}), now=now)
        view = await repo.get_stay_view(resource)
        with pytest.raises(ResourceNotReadyError):
            await TripUnderstandingApplicationService(repo).select_stay(resource, candidate_token=view.candidates[0].candidate_token,
                expected_etag=current.opaque_etag, idempotency_key="reject-excluded-branch", now=now)
        after = await repo.get_result(resource)
        assert after.opaque_etag == current.opaque_etag


def selected_view():
    return StayCandidateView(candidate_token="synthetic-selected-hotel-token", name="已选门店", brand="汉庭", category="住宿",
        area_or_address="合成地址", commute_summary="路线待核对", transfer_count=0, reason="已选", available_actions=[], selected=True)


def test_carried_selection_is_trimmed_per_current_night_without_mutating_history_or_reusing_old_segment_key():
    plan = checkout_plan()
    plan = plan.model_copy(update={"stops": [s.model_copy(update={"resolution_status": "UNRESOLVED"})
        if s.day_index == 2 else s for s in plan.stops]})
    original = {"selected_city": "北京", "selected_place_id": "北京-0", "overnight_days": [1, 2]}
    current = _current_selections([original], plan)
    assert current[0]["overnight_days"] == [2] and original["overnight_days"] == [1, 2]
    metadata = _overnight_metadata(plan)
    old_binding = {"segment_key": metadata[0]["segment_key"], "overnight_days": [1, 2]}
    assert not _has_selected_night({**old_binding, "overnight_days": [1]}, current)
    assert _has_selected_night({**old_binding, "overnight_days": [2]}, current)
    view = _segmented_view(metadata, [(selected_view(), _selection_binding(old_binding, current[0]))])
    assert view.segments[0].candidates == []
    assert view.segments[1].candidates[0].selected and view.segments[1].status == "LIMITED"
    assert old_binding["overnight_days"] == [1, 2]


@pytest.mark.parametrize("pending", ["exclusion", "role"])
def test_uncertain_source_keeps_the_user_choice_pending_without_claiming_the_replacement_is_satisfied(pending):
    plan = checkout_plan(confirmed=False)
    if pending == "role":
        plan = plan.model_copy(update={"stops": [plan.stops[0].model_copy(update={
            "lodging_excluded_nights": [], "lodging_role_uncertain": True, "lodging_event": None}), *plan.stops[1:]]})
    original = {"selected_city": "北京", "selected_place_id": "北京-0", "overnight_days": [1, 2]}
    current = _current_selections([original], plan)
    assert current[0]["overnight_days"] == [1, 2]
    view = _segmented_view(_overnight_metadata(plan), [(selected_view(), _selection_binding({}, current[0]))])
    first = view.segments[0]
    assert first.candidates[0].selected and first.status == "LIMITED"
    assert ("具体门店尚未核实" if pending == "exclusion" else "用途还需确认") in first.message
    assert "已按原文另住要求排除原门店" not in first.message


@pytest.mark.parametrize("kind", ["memory", "postgres"])
@pytest.mark.parametrize("legacy_carried", [False, True])
@pytest.mark.asyncio
async def test_source_identity_correction_invalidates_selected_old_branch_after_refresh_and_undo_restores_history(kind, legacy_carried):
    clause = "上午从北京饭店退房，然后游览故宫博物院，今晚另找一家酒店"
    source = f"北京三日游。Day1：{clause}。Day2：天坛公园。Day3：颐和园。"
    rows = [{**activity("北京饭店"), "category": "住宿", "lodging_event": "CHECK_OUT",
        "lodging_evidence": "上午从北京饭店退房", "lodging_excluded_nights": [1], "lodging_exclusion_evidence": clause},
        activity("故宫博物院"), activity("天坛公园", 2), activity("颐和园", 3)]
    output = await TripUnderstandingPipeline(DraftProvider(rows), RecordingResolver()).run(source)
    async with repository_for(kind) as repo:
        now = datetime.now(timezone.utc)
        created = await repo.create_demo(capability_hash="a"*64, source_text=source, idempotency_key="correct-old-hotel",
            request_hash=canonical_sha256({"text": source}), now=now, ttl_hours=24)
        job = await repo.claim_next(worker_id="old-hotel-source", now=now, lease_seconds=60)
        await repo.complete_job(job, output, now=now)
        resource = await repo.authorize(created.accepted.public_resource_id, capability_hash="a"*64, now=now)
        await finish_stay(repo, now)
        current = await repo.get_result(resource)
        candidate = (await repo.get_stay_view(resource)).candidates[0]
        service = TripUnderstandingApplicationService(repo)
        await service.select_stay(resource, candidate_token=candidate.candidate_token, expected_etag=current.opaque_etag,
            idempotency_key="choose-before-correction", now=now)
        resource, selected = await refresh(repo, resource, now)
        before, _ = await repo.get_current_place_plan(resource)
        selected_revision = before.plan_ref.revision
        anchors = [s for s in before.stops if s.is_stay_anchor]
        assert anchors and anchors[0].name == candidate.name
        selected_id = anchors[0].canonical_place_id
        source_hotel = selected.result.days[0].activities[0]
        assert source_hotel.lodging_excluded_nights == [1]
        if kind == "memory":
            history = dict(repo.stay_selections[(resource.understanding_id, selected_revision)])
        else:
            history = dict(await repo._pool.fetchrow("SELECT * FROM trip_stay_selections WHERE understanding_id=$1", resource.understanding_id))
        replacement = issue_candidate(CandidatePlace(canonical_place_id=f"amap:{selected_id}", city="北京", name=candidate.name,
            category="住宿", area_or_address=candidate.area_or_address,
            position=GCJ02Position(longitude=anchors[0].longitude, latitude=anchors[0].latitude)),
            public_resource_id=resource.public_resource_id, activity_token=source_hotel.activity_token,
            expected_etag=selected.opaque_etag, now=now)
        await service.apply_command(resource, PlaceConfirmCommand(command_type="PLACE_CONFIRM", activity_token=source_hotel.activity_token,
            candidate_token=replacement.candidate_token), expected_etag=selected.opaque_etag, idempotency_key="correct-source-hotel", now=now)
        resource, corrected = await refresh(repo, resource, now)
        corrected_plan, _ = await repo.get_current_place_plan(resource)
        corrected_revision = corrected_plan.plan_ref.revision
        assert corrected.result.days[0].activities[0].lodging_excluded_nights == [1]
        if kind == "memory":
            assert (resource.understanding_id, corrected_revision) not in repo.stay_selections
            if legacy_carried:
                repo.stay_selections[(resource.understanding_id, corrected_revision)] = dict(history)
        else:
            assert await repo._pool.fetchval("""SELECT count(*) FROM trip_stay_selections s JOIN trip_plan_revision_refs p
                ON p.plan_ref_id=s.target_plan_ref_id WHERE p.understanding_id=$1 AND p.revision=$2""", resource.understanding_id, corrected_revision) == 0
            if legacy_carried:
                async with repo._pool.acquire() as conn:
                    target, _ = await repo._ensure_plan_ref(conn, resource.understanding_id, corrected_revision, now=now)
                    await repo._insert_carried_stay(conn, history, target["plan_ref_id"], now=now)
        # Also protect GET against a row written by the old carry-forward code.
        assert not any(c.selected for s in (await repo.get_stay_view(resource)).segments for c in s.candidates)
        corrected_plan, _ = await repo.get_current_place_plan(resource)
        assert not any(s.is_stay_anchor for s in corrected_plan.stops)
        assert await job_count(repo, kind) == 1
        await repo.refresh_stay_suggestions(resource, expected_etag=corrected.opaque_etag, idempotency_key="refresh-corrected", now=now)
        await finish_stay(repo, now)
        refreshed = await repo.get_stay_view(resource)
        assert refreshed.candidates and all(not c.selected and c.name != candidate.name for c in refreshed.candidates)
        assert "已按原文另住要求排除原门店" in refreshed.segments[0].message
        assert (await repo.get_result(resource)).opaque_etag == corrected.opaque_etag
        await service.apply_command(resource, UndoCommand(command_type="UNDO"), expected_etag=corrected.opaque_etag,
            idempotency_key="undo-source-correction", now=now)
        resource, undone = await refresh(repo, resource, now)
        assert undone.result.days[0].activities[0].name == source_hotel.name
        restored = await repo.get_stay_view(resource)
        assert restored.candidates[0].selected and restored.candidates[0].name == candidate.name
        restored_plan, _ = await repo.get_current_place_plan(resource)
        assert {s.canonical_place_id for s in restored_plan.stops if s.is_stay_anchor} == {selected_id}
        if kind == "memory":
            assert repo.stay_selections[(resource.understanding_id, selected_revision)]["overnight_days"] == history["overnight_days"] == [1, 2]
        else:
            assert await repo._pool.fetchval("SELECT overnight_days FROM trip_stay_selections WHERE selection_id=$1",
                history["selection_id"]) == history["overnight_days"] == [1, 2]
