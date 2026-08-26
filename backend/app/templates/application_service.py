from __future__ import annotations

from datetime import datetime, time, timezone
from uuid import NAMESPACE_URL, uuid4, uuid5

from app.itineraries.errors import RevisionConflictError, ResourceNotFound
from app.itineraries.hash_service import sha256_canonical, with_content_hash
from app.itineraries.models import ItineraryDay, ItineraryRevisionContent, ItineraryStop, ResolutionStatus, RevisionSource
from app.itineraries.repositories import ItineraryRepository
from app.operations.models import CreationCommandResponse, CreationOperation
from app.operations.repositories import CreationCommandRepository
from app.templates.models import CityRouteTemplate


def _duration_minutes(time_window: str, fallback: int) -> tuple[str, str, int]:
    start_text, end_text = time_window.split("-", 1)
    start, end = time.fromisoformat(start_text), time.fromisoformat(end_text)
    minutes = (end.hour * 60 + end.minute) - (start.hour * 60 + start.minute)
    return start_text, end_text, max(0, min(fallback, minutes))


class TemplateApplicationService:
    """Turns a skeleton into revision 1 without treating it as a verified plan."""

    def __init__(self, itinerary_repository: ItineraryRepository):
        self.itinerary_repository = itinerary_repository

    def _revision(self, *, workspace_id: str, template: CityRouteTemplate, actor_user_id: str, date_range):
        day_count = (date_range.end - date_range.start).days + 1
        stops_by_day: dict[int, list[ItineraryStop]] = {index: [] for index in range(day_count)}
        anchor_places = {place.place_id: place for place in template.anchor_places}
        anchor_projection: dict[str, dict] = {}
        # Only explicitly stable canonical anchors become stops.  Dynamic food
        # and hotel choices remain slots/alternative groups and are resolved at
        # suggestion time rather than being baked into the template.
        for slot in template.anchor_slots:
            if slot.day_offset >= day_count:
                continue
            for place_id in slot.anchor_place_ids:
                start, end, duration = _duration_minutes(slot.time_window, slot.dwell_minutes)
                order = len(stops_by_day[slot.day_offset])
                stop_id = str(uuid5(NAMESPACE_URL, f"{workspace_id}:{template.template_id}:{template.template_version}:{slot.slot_id}:{place_id}"))
                anchor = anchor_places[place_id]
                stops_by_day[slot.day_offset].append(ItineraryStop(
                    stop_id=stop_id,
                    place_id=place_id,
                    day_index=slot.day_offset,
                    order_index=order,
                    start_time=start,
                    end_time=end,
                    visit_duration_minutes=duration,
                    raw_name=anchor.name,
                    # A model DRAFT is usable as a local projection but is not
                    # a user confirmation or a real-POI resolution.
                    resolution_status=ResolutionStatus.AMBIGUOUS,
                    category=slot.category_constraints[0] if slot.category_constraints else "attraction",
                    notes="MODEL_GENERATED_DRAFT; SYNTHETIC_ROUTE_ANCHOR; requires POI replacement before confirmation",
                ))
                anchor_projection[stop_id] = {
                    "place": anchor.model_dump(mode="json"),
                    "provenance": template.provenance.value,
                    "human_review_evidence": False,
                    "coordinate_role": "SYNTHETIC_TEMPLATE_ANCHOR",
                }
        days = [
            ItineraryDay(day_index=index, date=date_range.start.fromordinal(date_range.start.toordinal() + index), stops=stops_by_day[index])
            for index in range(day_count)
        ]
        return with_content_hash(ItineraryRevisionContent(
            itinerary_id=str(uuid4()), workspace_id=workspace_id, revision=1,
            source_type=RevisionSource.TEMPLATE, city=template.city, date_range=date_range,
            days=days,
            change_summary={
                "operation": "APPLY_TEMPLATE", "template_id": template.template_id,
                "template_version": template.template_version, "template_status": template.status.value,
                "template_provenance": template.provenance.value,
                "human_review_evidence": False,
                "template_anchor_places": anchor_projection,
            },
            created_by=actor_user_id, created_at=datetime.now(timezone.utc),
        ))

    async def apply_idempotent(
        self,
        *,
        workspace_id: str,
        template: CityRouteTemplate,
        actor_user_id: str,
        idempotency_key: str,
        command_repository: CreationCommandRepository,
    ) -> tuple[dict, bool]:
        workspace = await self.itinerary_repository.get_workspace(workspace_id)
        if workspace is None:
            raise ResourceNotFound("workspace does not exist")
        if workspace.city != template.city:
            raise RevisionConflictError("template city does not match workspace", context={"workspace_city": workspace.city, "template_city": template.city})
        request_hash = sha256_canonical({
            "schema_version": 1, "operation": CreationOperation.APPLY_TEMPLATE.value,
            "workspace_id": workspace_id, "template_id": template.template_id,
            "template_version": template.template_version, "actor_user_id": actor_user_id,
        })
        claim = await command_repository.claim(
            workspace_id=workspace_id, operation=CreationOperation.APPLY_TEMPLATE,
            target_id=template.template_id, actor_user_id=actor_user_id,
            idempotency_key=idempotency_key, request_hash=request_hash,
            basis={"template_id": template.template_id, "template_version": template.template_version},
        )
        if claim.replay is not None:
            return dict(claim.replay.body), True
        try:
            # The basis is frozen by the ledger.  A later template update cannot
            # silently alter a retry's initial itinerary.
            if workspace.current_itinerary_revision is not None:
                raise RevisionConflictError("workspace already has an itinerary", context={"actual_revision": workspace.current_itinerary_revision})
            revision = self._revision(
                workspace_id=workspace_id, template=template, actor_user_id=actor_user_id,
                date_range=workspace.trip_date_range,
            )

            async def finalize(_conn, _basis):
                attached = await self.itinerary_repository.attach_initial_revision_in_transaction(
                    _conn, workspace_id, revision
                )
                return CreationCommandResponse(
                    status_code=201,
                    body={
                        "workspace_id": workspace_id, "template_id": template.template_id,
                        "template_version": template.template_version,
                        "revision": revision.model_dump(mode="json"),
                        "workspace": attached.model_dump(mode="json"),
                        "template_provenance": template.provenance.value,
                        "human_review_evidence": False,
                    },
                    headers={"ETag": '"1"', "Cache-Control": "no-store"},
                )

            response = await command_repository.finalize(claim, finalize)
            return dict(response.body), response.idempotent_replay
        except Exception:
            await command_repository.abandon(claim)
            raise
