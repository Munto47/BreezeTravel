from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app.audit.models import (
    AuditFinding,
    AuditReport,
    AuditSeverity,
    AuditStatus,
    EvidenceSnapshot,
)
from app.api.trip_understandings_v3 import get_trip_understanding_repository
from app.main import app
from app.trip_understanding.g03 import calendar_profile, public_checks
from app.trip_understanding.map_worker import MapRenderWorker
from app.trip_understanding.repository import InMemoryTripUnderstandingRepository
from app.trip_understanding.worker import TripUnderstandingWorker
from app.utils.auth import get_current_user


FORBIDDEN_PUBLIC_TERMS = (
    "revision",
    "receipt",
    "evidence",
    "audit",
    "repair",
    "postcheck",
    "hash",
    "uid",
)


def _public_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False).lower()


def test_calendar_modes_and_party_source_are_explicit_without_fake_dates() -> None:
    day_index = calendar_profile([], day_count=3)
    assert day_index.mode == "DAY_INDEX_ONLY"
    assert day_index.start is None
    assert day_index.end is None
    assert day_index.party_size == 2
    assert day_index.party_size_source == "DEFAULT_TWO"

    dated = calendar_profile(
        [
            {
                "key": "calendar",
                "value": "2026-09-01 至 2026-09-03",
                "source": "USER_EDIT",
            },
            {"key": "party_size", "value": "4 人", "source": "USER_EDIT"},
        ],
        day_count=3,
    )
    assert dated.mode == "ABSOLUTE_DATES"
    assert dated.start and dated.start.isoformat() == "2026-09-01"
    assert dated.end and dated.end.isoformat() == "2026-09-03"
    assert dated.party_size == 4
    assert dated.party_size_source == "USER_PROVIDED"


def _audit_report(findings: list[AuditFinding]) -> tuple[AuditReport, EvidenceSnapshot]:
    observed_at = datetime(2026, 8, 30, tzinfo=timezone.utc)
    snapshot = EvidenceSnapshot(
        snapshot_id="snapshot-g03-ordering",
        workspace_id="workspace-g03-ordering",
        itinerary_revision=1,
        policy_version="g03-evidence-v1",
        created_at=observed_at,
    )
    return (
        AuditReport(
            report_id="report-g03-ordering",
            workspace_id="workspace-g03-ordering",
            itinerary_id="itinerary-g03-ordering",
            itinerary_revision=1,
            task_id="task-g03-ordering",
            task_revision=1,
            evidence_snapshot_id=snapshot.snapshot_id,
            audit_rule_set_version="g03-test",
            report_input_hash="a" * 64,
            overall_status=AuditStatus.VIOLATED,
            findings=findings,
            created_at=observed_at,
        ),
        snapshot,
    )


def _finding(
    finding_id: str,
    *,
    status: AuditStatus,
    severity: AuditSeverity,
    reason_code: str,
    day: int,
    repairable: bool = False,
) -> AuditFinding:
    return AuditFinding(
        finding_id=finding_id,
        rule_id=f"g03.test.{finding_id}",
        rule_version="1.0.0",
        status=status,
        severity=severity,
        reason_code=reason_code,
        message=finding_id,
        affected_days=[day],
        affected_stop_ids=[f"stop-{finding_id}"],
        repairable=repairable,
    )


def test_public_top3_mapping_order_and_resolved_item_backfill_are_stable() -> None:
    mapped_findings = [
        _finding(
            "must",
            status=AuditStatus.VIOLATED,
            severity=AuditSeverity.HIGH,
            reason_code="ROUTE_TOO_LONG",
            day=0,
        ),
        _finding(
            "confirm",
            status=AuditStatus.UNKNOWN,
            severity=AuditSeverity.MEDIUM,
            reason_code="OPENING_CONFIRMATION_REQUIRED",
            day=1,
        ),
        _finding(
            "improve",
            status=AuditStatus.VIOLATED,
            severity=AuditSeverity.MEDIUM,
            reason_code="MEAL_BREAK_MISSING",
            day=2,
            repairable=True,
        ),
        _finding(
            "low",
            status=AuditStatus.VIOLATED,
            severity=AuditSeverity.LOW,
            reason_code="STAY_COMMUTE_LONG",
            day=0,
        ),
    ]
    report, snapshot = _audit_report(mapped_findings)
    tokens = {item.finding_id: f"token-{item.finding_id}-0123456789" for item in mapped_findings}
    view = public_checks(report, snapshot, check_tokens=tokens)
    assert [item.check_token for item in view.items] == [
        tokens["must"],
        tokens["improve"],
        tokens["confirm"],
    ]
    assert [item.label for item in view.items] == ["必须调整", "可以更好", "需要确认"]

    hard_findings = [
        _finding(
            f"hard-{index}",
            status=AuditStatus.VIOLATED,
            severity=AuditSeverity.HIGH,
            reason_code="ROUTE_TOO_LONG",
            day=index,
        )
        for index in range(4)
    ]
    hard_report, hard_snapshot = _audit_report(hard_findings)
    hard_tokens = {
        item.finding_id: f"token-{item.finding_id}-0123456789" for item in hard_findings
    }
    first = public_checks(hard_report, hard_snapshot, check_tokens=hard_tokens)
    assert [item.check_token for item in first.items] == [
        hard_tokens["hard-0"],
        hard_tokens["hard-1"],
        hard_tokens["hard-2"],
    ]
    assert first.remaining_must_adjust == 1

    resolved_report = hard_report.model_copy(
        update={
            "findings": [
                hard_findings[0].model_copy(update={"status": AuditStatus.SATISFIED}),
                *hard_findings[1:],
            ]
        }
    )
    refilled = public_checks(resolved_report, hard_snapshot, check_tokens=hard_tokens)
    assert [item.check_token for item in refilled.items] == [
        hard_tokens["hard-1"],
        hard_tokens["hard-2"],
        hard_tokens["hard-3"],
    ]
    assert refilled.remaining_must_adjust == 0


def test_g03_public_materialize_top3_preview_adopt_and_full_postcheck() -> None:
    repository = InMemoryTripUnderstandingRepository()
    app.dependency_overrides[get_trip_understanding_repository] = lambda: repository
    client = TestClient(app)
    try:
        created = client.post(
            "/api/v3/trip-understandings",
            headers={"Idempotency-Key": "g03-create"},
            json={"mode": "DEMO"},
        )
        assert created.status_code == 202
        resource_id = created.json()["public_resource_id"]
        asyncio.run(TripUnderstandingWorker(repository).run_once("g03-understanding"))
        result = client.get(
            f"/api/v3/trip-understandings/{resource_id}/result"
        )
        assert result.status_code == 200
        initial_etag = result.headers["etag"]

        map_worker = MapRenderWorker(repository)
        assert asyncio.run(map_worker.run_once("g03-map")) is True
        provider_effects_before = repository.map_provider_effect_count

        anonymous_materialized = client.post(
            f"/api/v3/trip-understandings/{resource_id}/materialize",
            headers={
                "Idempotency-Key": "g03-materialize-without-login",
                "If-Match": initial_etag,
            },
        )
        assert anonymous_materialized.status_code == 200
        app.dependency_overrides[get_current_user] = lambda: "g03-owner"

        materialized = client.post(
            f"/api/v3/trip-understandings/{resource_id}/materialize",
            headers={
                "Idempotency-Key": "g03-materialize",
                "If-Match": initial_etag,
            },
        )
        assert materialized.status_code == 200
        assert materialized.json() == {
            "status": "READY",
            "message": "行程已准备好，可以查看最值得处理的三项",
            "calendar": "按 Day 编号安排",
            "party_size": 2,
            "checks_available": True,
        }
        assert materialized.headers["etag"] == initial_etag

        replay = client.post(
            f"/api/v3/trip-understandings/{resource_id}/materialize",
            headers={
                "Idempotency-Key": "g03-materialize",
                "If-Match": initial_etag,
            },
        )
        assert replay.headers["Idempotency-Replayed"] == "true"
        second_key = client.post(
            f"/api/v3/trip-understandings/{resource_id}/materialize",
            headers={
                "Idempotency-Key": "g03-materialize-second-key",
                "If-Match": initial_etag,
            },
        )
        assert second_key.status_code == 200
        history = repository.g03_history[repository.resources[resource_id]["understanding_id"]]
        assert len(history) == 3  # New operation keys explicitly refresh evidence.
        assert len({item["itinerary"].content_hash for item in history}) == 1
        assert len({item["report"].report_id for item in history}) == 3

        checks = client.get(
            f"/api/v3/trip-understandings/{resource_id}/checks"
        )
        assert checks.status_code == 200
        check_payload = checks.json()
        assert len(check_payload["items"]) == 3
        assert [item["affected_days"] for item in check_payload["items"]] == [
            ["Day 1"],
            ["Day 2"],
            ["Day 3"],
        ]
        assert all(item["label"] == "可以更好" for item in check_payload["items"])
        assert all(item["can_preview"] for item in check_payload["items"])
        internal_state = repository.g03_materialized[
            repository.resources[resource_id]["understanding_id"]
        ]
        meal_findings = [
            finding
            for finding in internal_state["report"].findings
            if finding.reason_code == "MEAL_BREAK_MISSING"
        ]
        assert len(meal_findings) == 3
        assert not any(
            finding.reason_code == "OPENING_CONFIRMATION_REQUIRED"
            for finding in internal_state["report"].findings
        )

        check_token = check_payload["items"][0]["check_token"]
        preview = client.post(
            f"/api/v3/trip-understandings/{resource_id}/changes/preview",
            headers={"Idempotency-Key": "g03-preview"},
            json={"check_token": check_token},
        )
        assert preview.status_code == 200
        assert preview.json()["affected_days"] == ["Day 1"]
        assert preview.json()["available_actions"] == ["ADOPT_CHANGE"]

        adopted = client.post(
            f"/api/v3/trip-understandings/{resource_id}/changes/adopt",
            headers={
                "Idempotency-Key": "g03-adopt",
                "If-Match": initial_etag,
            },
            json={"change_token": preview.json()["change_token"]},
        )
        assert adopted.status_code == 200
        adopted_payload = adopted.json()
        assert adopted_payload["status"] == "STILL_NEEDS_CONFIRMATION"
        assert adopted_payload["map_readiness"] == "NEEDS_UPDATE"
        assert adopted_payload["changed_days"] == ["Day 1"]
        assert adopted.headers["etag"] != initial_etag
        assert repository.map_provider_effect_count == provider_effects_before

        refreshed = client.get(
            f"/api/v3/trip-understandings/{resource_id}/result"
        )
        assert refreshed.headers["etag"] == adopted.headers["etag"]
        assert refreshed.json()["map"]["status"] == "NEEDS_UPDATE"
        assert refreshed.json()["days"][0]["activities"][1]["name"] == "午餐时间"
        assert client.get(
            f"/api/v3/trip-understandings/{resource_id}/checks"
        ).json() == adopted_payload["checks"]

        adopted_replay = client.post(
            f"/api/v3/trip-understandings/{resource_id}/changes/adopt",
            headers={
                "Idempotency-Key": "g03-adopt",
                "If-Match": initial_etag,
            },
            json={"change_token": preview.json()["change_token"]},
        )
        assert adopted_replay.status_code == 200
        assert adopted_replay.headers["Idempotency-Replayed"] == "true"
        assert adopted_replay.headers["etag"] == adopted.headers["etag"]

        for public_payload in (
            materialized.json(),
            check_payload,
            preview.json(),
            adopted_payload,
        ):
            serialized = _public_text(public_payload)
            assert all(term not in serialized for term in FORBIDDEN_PUBLIC_TERMS)
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_trip_understanding_repository, None)
