"""Complete read-only sharing; fixed structured additions are display fixtures.

The base result uses a fixed Provider/Pipeline. Added detail/alternative/coverage
fields below deliberately test projection, not model extraction accuracy.
"""

from datetime import UTC, datetime, timedelta
import hashlib

import pytest

from app.trip_understanding.memory_share import build_share_projection, ShareProjectionView
from app.trip_understanding.models import (
    ActivityAlternativeView,
    SourceDetailView,
    TripDayView,
    MealSlotView,
    UserFacingTripResult,
    ProposedMention,
    LodgingConstraintView,
)
from app.trip_understanding.pipeline import TripUnderstandingPipeline
from app.trip_understanding.demo import FixedBeijingPlaceResolver
from app.trip_understanding.readback import SupplementaryView, SupplementaryDay, SupplementaryItem
from tests.test_source_meal_selection import SOURCE, build_source_meal_result

FIXTURE_SOURCE = (
    SOURCE + "\nDay3：备选中国国家博物馆。早餐：豆浆和包子。\n全程备选：北京动物园。\n私有整篇哨兵，不进入分享。"
)


async def build_share_export_output():
    """Base fixed model plus explicit typed fixture additions, not live AI."""
    initial = await build_source_meal_result()
    plan = initial.proposal.model_copy(deep=True)
    plan.source_hash = hashlib.sha256(FIXTURE_SOURCE.encode()).hexdigest()
    plan.day_count, plan.day_labels = 3, {1: "Day 1", 2: "Day 2", 3: "Day 3"}
    for name, day, role, meal in [
        ("中国国家博物馆", 3, "OPTIONAL", None),
        ("早餐：豆浆和包子", 3, "PLANNED", "BREAKFAST"),
        ("北京动物园", None, "OPTIONAL", None),
    ]:
        start = FIXTURE_SOURCE.index(name)
        mention = ProposedMention(
            mention_id=f"share-fixture-{len(plan.mentions)}",
            raw_text=name,
            span_start=start,
            span_end=start + len(name),
            role=role,
            day_index=day,
            sequence_index=len(plan.mentions),
            atomic_place_name=None if meal else name,
            category_hint="餐饮" if meal else "景点",
            meal_role=meal,
        )
        plan.mentions.append(mention)
        if day is None:
            plan.unassigned_alternative_ids.append(mention.mention_id)

    class NoExtraModel:
        async def propose(self, _source):
            raise AssertionError("prepared fixture must not call another model")

    return await TripUnderstandingPipeline(NoExtraModel(), FixedBeijingPlaceResolver()).run(
        FIXTURE_SOURCE, prepared_plan=plan
    )


async def build_share_export_fixture(output=None):
    output = output or await build_share_export_output()
    result = output.public_result.model_copy(deep=True)
    result.days[0].activities[0].source_details = [
        SourceDetailView(name="入口：从午门进"),
        SourceDetailView(name="太和殿"),
        SourceDetailView(name="出口：从神武门出"),
    ]
    result.days[1].activities[0].status = "NEEDS_CONFIRMATION"
    result.days[1].activities[0].source_details = [SourceDetailView(name="祈年殿")]
    result.days[1].unprocessed_count = 2
    lodging = result.days[0].activities[0].model_dump()
    lodging.update(
        activity_token="share-fixture-hotel-00000001",
        name="固定北京全程住宿酒店",
        category="住宿",
        lodging_event="OVERNIGHT",
        lodging_scope="WHOLE_TRIP",
        source_details=[{"name": "入住后寄放行李", "optional": False}],
        scope="NIGHTS",
        overnight_days=[1, 2],
    )
    result.lodging_constraints = [LodgingConstraintView.model_validate(lodging)]
    result.days[2] = TripDayView(
        label="Day 3",
        activities=[],
        unprocessed_count=1,
        alternatives=[
            ActivityAlternativeView(
                name="中国国家博物馆",
                category="景点",
                branch_label="方案甲",
                source_details=[
                    SourceDetailView(name="古代中国展厅"),
                    SourceDetailView(name="复兴之路展厅", optional=True),
                ],
            )
        ],
        meal_slots=[MealSlotView(meal_role="BREAKFAST", selection_status="UNSELECTED", preference_text="豆浆和包子")],
    )
    result.status = "PARTIAL_RESULT"
    result.coverage.unresolved_place_count = 1
    result.coverage.confirmed_place_count = 3
    result.coverage.unclassified_mention_count = 1
    result.coverage.unprocessed_count = 3
    result.coverage.complete = False
    result = UserFacingTripResult.model_validate(result.model_dump())
    supplementary = SupplementaryView(
        status="AVAILABLE",
        days=[
            SupplementaryDay(
                day_index=None, day_label="未指定日期", items=[SupplementaryItem(name="北京动物园", role="OPTIONAL")]
            )
        ],
    )
    return result, supplementary


@pytest.mark.asyncio
async def test_current_share_preserves_every_day_optional_detail_meal_and_incomplete_state():
    result, supplementary = await build_share_export_fixture()
    original = result.model_dump_json()
    shared = build_share_projection(result)
    text = shared.model_dump_json()
    for name in ("中国国家博物馆", "古代中国展厅", "太和殿", "豆浆和包子", "本帮菜"):
        assert name in text
    assert len(shared.days) == 3 and shared.days[2].activities == []
    assert shared.days[1].pending_count == 1 and shared.days[1].unprocessed_count == 2
    assert shared.warnings
    assert "固定北京全程住宿酒店 · 第1、2晚 · 已确认" in shared.lodging_arrangements
    assert result.model_dump_json() == original


@pytest.mark.asyncio
async def test_share_global_alternatives_are_not_fabricated_as_day_one_and_have_no_private_fields():
    result, supplementary = await build_share_export_fixture()
    view = build_share_projection(result, supplementary=supplementary)
    assert view.unassigned_alternatives == ["北京动物园"]
    assert all("北京动物园" not in str(day.model_dump()) for day in view.days)
    public = view.model_dump_json()
    for forbidden in (
        "activity_token",
        "candidate_token",
        "source_quote",
        "public_resource_id",
        "canonical_place_id",
        "revision",
        "hash",
    ):
        assert forbidden not in public
    assert all(activity.time_hint is None for day in view.days for activity in day.activities)


def test_old_share_without_new_fields_remains_readable_and_does_not_gain_a_complete_claim():
    view = ShareProjectionView.model_validate(
        {
            "title": "旧行程",
            "destination": "北京",
            "schedule": "按天安排",
            "party_size": "2人",
            "days": [{"label": "Day 1", "activities": []}],
        }
    )
    assert view.days[0].alternatives == [] and view.days[0].meal_arrangements == []
    assert view.warnings == [] and view.unassigned_alternatives == []


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["memory", "postgres"])
async def test_complete_share_is_immutable_private_until_exchange_and_revocable_without_modifying_trip(kind):
    from app.trip_understanding.errors import ResourceNotFoundError, ResourceAccessDeniedError, RevisionConflictError
    from app.trip_understanding.models import CreateFullRequest, ActivityMoveCommand
    from app.trip_understanding.service import TripUnderstandingApplicationService
    from tests.test_experience_v3_journey import repository_for

    async with repository_for(kind) as repo:
        now = datetime.now(UTC)
        service = TripUnderstandingApplicationService(repo)
        created = await service.create_full(
            CreateFullRequest.model_validate({"mode": "FULL", "source": {"type": "TEXT", "text": FIXTURE_SOURCE}}),
            owner_user_id="experience-owner",
            idempotency_key="complete-share-trip",
            now=now,
        )
        job = await repo.claim_next(worker_id="fixed-share", now=now, lease_seconds=60)
        output = await build_share_export_output()
        output.public_result, _ = await build_share_export_fixture(output)
        await repo.complete_job(job, output, now=now)
        resource = await repo.authorize(
            created.accepted.public_resource_id, capability_hash=None, user_id="experience-owner", now=now
        )
        before = await repo.get_result(resource)
        supplementary = await repo.get_supplementary_view(resource, now=now)
        assert [item.name for day in supplementary.days if day.day_index is None for item in day.items] == [
            "北京动物园"
        ]
        kwargs = dict(
            idempotency_key="complete-share",
            expires_in_days=7,
            signing_key="fixed-test-share-key",
            now=now,
            supplementary=supplementary,
        )
        with pytest.raises(ResourceAccessDeniedError):
            await repo.create_share(resource, "other-owner", before.result, **kwargs)
        share, replayed = await repo.create_share(resource, "experience-owner", before.result, **kwargs)
        assert not replayed
        duplicate, replayed = await repo.create_share(resource, "experience-owner", before.result, **kwargs)
        assert duplicate == share and replayed
        path, secret = share.share_url.split("#s=")
        ref = path.rsplit("/", 1)[-1]
        with pytest.raises(ResourceNotFoundError):
            await repo.read_share(ref, None, now=now)
        with pytest.raises(ResourceNotFoundError):
            await repo.exchange_share_secret(ref, "wrong-secret", now=now)
        session = await repo.exchange_share_secret(ref, secret, now=now)
        view = await repo.read_share(ref, session.capability, now=now)
        assert view.unassigned_alternatives == ["北京动物园"]
        assert "古代中国展厅" in view.model_dump_json() and "豆浆和包子" in view.model_dump_json()
        assert "私有整篇哨兵" not in view.model_dump_json()
        after = await repo.get_result(resource)
        assert after.result == before.result and after.opaque_etag == before.opaque_etag
        await service.apply_command(
            resource,
            ActivityMoveCommand(
                command_type="ACTIVITY_MOVE",
                activity_token=before.result.days[0].activities[0].activity_token,
                target_day_index=1,
                target_position=1,
            ),
            expected_etag=before.opaque_etag,
            idempotency_key="edit-after-sharing",
            now=now,
        )
        assert await repo.read_share(ref, session.capability, now=now) == view
        with pytest.raises(RevisionConflictError):
            await repo.create_share(
                resource, "experience-owner", before.result, **{**kwargs, "idempotency_key": "stale-share"}
            )
        with pytest.raises(ResourceNotFoundError):
            await repo.read_share(ref, session.capability, now=now + timedelta(hours=2))
        assert not await repo.revoke_share(ref, "other-owner", now=now)
        assert await repo.revoke_share(ref, "experience-owner", now=now)
        with pytest.raises(ResourceNotFoundError):
            await repo.read_share(ref, session.capability, now=now)
        with pytest.raises(ResourceNotFoundError):
            await repo.exchange_share_secret(ref, secret, now=now)
