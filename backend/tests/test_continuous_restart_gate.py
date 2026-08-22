from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

from evals.continuous.restart_gate import validate_restart_evidence


def _json_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _case(index: int, city: str) -> dict:
    revision = 2
    content_hash = f"{index + 1:064x}"
    report_id = f"report-{index}"
    event = {
        "event_id": f"event-{index}",
        "event_type": "suggestions_shown",
        "workspace_id": f"workspace-{index}",
        "itinerary_revision": revision,
    }
    map_projection = {
        "workspace_id": f"workspace-{index}",
        "revision": revision,
        "city": city,
        "status": "AVAILABLE",
        "stops": [
            {"stop_id": f"stop-{index}-a", "place_id": f"place-{index}-a"},
            {"stop_id": f"stop-{index}-b", "place_id": f"place-{index}-b"},
        ],
        "coordinate_links": [],
        "missing_stop_ids": [],
        "unavailable_reason": None,
    }
    authoritative = {
        "resume": {
            "revision": revision,
            "content_hash": content_hash,
            "report_id": report_id,
            "report_revision": revision,
            "report_status": "PASS",
            "member_constraint_revision": 1,
        },
        "members": [{"member_id": "a"}, {"member_id": "b"}],
        "map_projection": map_projection,
        "map_projection_sha256": _json_hash(map_projection),
        "recommendation_events": [event],
    }
    yjs = {
        "itinerary_revision": revision,
        "itinerary_content_hash": content_hash,
        "audit_report_id": report_id,
        "audit_revision": revision,
        "member_constraint_revision": 1,
        "map_revision": revision,
        "map_projection_sha256": authoritative["map_projection_sha256"],
        "places": [{"place_id": f"place-{index}", "value": {"note": "edited-by-b"}}],
        "builder_events": [
            {"event_id": f"builder-{index}", "backend_event_ids": [event["event_id"]]}
        ],
    }
    browser = {"browser_a": deepcopy(authoritative), "browser_b": deepcopy(authoritative)}
    assertions = {
        "independent_seed_and_storage_keys": True,
        "exact_revision_and_content_hash": True,
        "exact_audit_report_and_revision": True,
        "exact_member_constraints_and_revision": True,
        "exact_available_map_projection_and_hash": True,
        "exact_nonempty_recommendation_event_ledger": True,
        "exact_yjs_places_and_builder_events": True,
        "fresh_yjs_read_preceded_browser_reconnect": True,
        "two_fresh_browser_contexts_match_authority": True,
    }
    return {
        "case_id": f"g5.case-{index}",
        "seed_id": f"seed-{index}",
        "room_id": f"room-{index}",
        "workspace_id": f"workspace-{index}",
        "city": city,
        "operation": "REORDER_STOP",
        "status": "PASS",
        "expected": {"authoritative": deepcopy(authoritative), "yjs": deepcopy(yjs)},
        "before_restart": {
            "authoritative_http": deepcopy(authoritative),
            "browser": deepcopy(browser),
            "yjs_fresh_client": deepcopy(yjs),
        },
        "after_restart": {
            "authoritative_http": deepcopy(authoritative),
            "browser": deepcopy(browser),
            "yjs_fresh_client_before_browser": deepcopy(yjs),
        },
        "assertions": assertions,
    }


def _evidence() -> tuple[dict, datetime]:
    launched = datetime.now(timezone.utc)
    started = launched + timedelta(milliseconds=10)
    cities = ["北京"] * 3 + ["上海"] * 3 + ["杭州"] * 3
    assertion_names = {
        "exactly_nine_independent_cases",
        "all_pre_restart_browser_contexts_closed",
        "backend_process_restarted",
        "yjs_process_restarted_with_same_named_volume",
        "backend_and_yjs_boot_generation_changed",
        "stopped_ports_were_unavailable_before_start",
        "postgres_container_not_restarted",
        "all_fresh_yjs_reads_preceded_browser_reconnect",
        "all_case_http_yjs_browser_refs_recovered_exactly",
        "public_http_and_authenticated_yjs_only",
    }
    payload = {
        "schema_version": "3.0",
        "status": "PASSED",
        "started_at": started.isoformat(),
        "finished_at": (started + timedelta(seconds=1)).isoformat(),
        "safety_contract": {
            "direct_domain_calls": 0,
            "direct_sql_calls": 0,
            "direct_leveldb_calls": 0,
            "repository_rebuild_substitute": False,
        },
        "services": {
            "boot_before": {
                "backend": {"instance_id": "00000000-0000-4000-8000-000000000001", "started_at": started.isoformat(), "pid": 1},
                "y_websocket": {"instance_id": "00000000-0000-4000-8000-000000000003", "started_at": started.isoformat(), "pid": 1},
            },
            "boot_after": {
                "backend": {"instance_id": "00000000-0000-4000-8000-000000000002", "started_at": (started + timedelta(milliseconds=1)).isoformat(), "pid": 2},
                "y_websocket": {"instance_id": "00000000-0000-4000-8000-000000000004", "started_at": (started + timedelta(milliseconds=1)).isoformat(), "pid": 2},
            },
            "before_restart": {
                "backend": {"id": "backend", "host_pid": 10, "started_at": started.isoformat()},
                "y_websocket": {"id": "yjs", "host_pid": 11, "started_at": started.isoformat()},
                "postgres": {"id": "postgres", "host_pid": 12, "started_at": started.isoformat()},
            },
            "after_restart": {
                "backend": {"id": "backend", "host_pid": 20, "started_at": (started + timedelta(milliseconds=1)).isoformat()},
                "y_websocket": {"id": "yjs", "host_pid": 21, "started_at": (started + timedelta(milliseconds=1)).isoformat()},
                "postgres": {"id": "postgres", "host_pid": 12, "started_at": started.isoformat()},
            },
            "stopped_ports_observed_unavailable": True,
            "yjs_named_volume_preserved": "agenttravel_yjs-data",
        },
        "cases": [_case(index, city) for index, city in enumerate(cities)],
        "assertions": {key: True for key in assertion_names},
        "cleanup": {
            "postgres": "CLEARED",
            "postgres_room_count": 9,
            "yjs_documents": "CLEARED",
            "yjs_room_count": 9,
        },
    }
    return payload, launched


def test_restart_evidence_accepts_nine_exact_http_browser_and_yjs_readbacks():
    payload, launched = _evidence()
    result = validate_restart_evidence(payload, launched_at=launched)
    assert result.passed, result.receipt["errors"]
    assert result.receipt["schema_version"] == "backend-yjs-restart-gate-v2"
    assert result.receipt["claim_scope"] == "local_fixture_public_http_yjs_browser"


def test_restart_evidence_rejects_unchanged_process_generation():
    payload, launched = _evidence()
    payload["services"]["boot_after"]["backend"] = deepcopy(
        payload["services"]["boot_before"]["backend"]
    )
    payload["services"]["after_restart"]["backend"] = deepcopy(
        payload["services"]["before_restart"]["backend"]
    )
    result = validate_restart_evidence(payload, launched_at=launched)
    assert not result.passed
    assert "BACKEND_BOOT_INSTANCE_UNCHANGED" in result.receipt["errors"]
    assert "BACKEND_HOST_PID_UNCHANGED" in result.receipt["errors"]


def test_restart_evidence_rejects_eight_cases_or_duplicate_seed():
    payload, launched = _evidence()
    payload["cases"].pop()
    payload["cases"][1]["seed_id"] = payload["cases"][0]["seed_id"]
    result = validate_restart_evidence(payload, launched_at=launched)
    assert not result.passed
    assert "EXACTLY_NINE_RECOVERY_CASES_REQUIRED" in result.receipt["errors"]
    assert "CASE_SEED_IDS_NOT_UNIQUE" in result.receipt["errors"]


def test_restart_evidence_rejects_one_case_map_or_yjs_mismatch():
    payload, launched = _evidence()
    payload["cases"][4]["expected"]["yjs"]["map_projection_sha256"] = "f" * 64
    payload["cases"][4]["after_restart"]["yjs_fresh_client_before_browser"]["places"] = []
    result = validate_restart_evidence(payload, launched_at=launched)
    assert not result.passed
    assert "CASE_5_YJS_AUTHORITY_REF_MISMATCH" in result.receipt["errors"]
    assert "CASE_5_POST_RESTART_YJS_MISMATCH" in result.receipt["errors"]


def test_restart_evidence_rejects_incomplete_cleanup():
    payload, launched = _evidence()
    payload["cleanup"]["yjs_room_count"] = 8
    result = validate_restart_evidence(payload, launched_at=launched)
    assert not result.passed
    assert "ISOLATED_TEST_STATE_NOT_CLEARED" in result.receipt["errors"]


def test_builder_run_spec_binds_the_nine_case_restart_matrix():
    repo_root = Path(__file__).resolve().parents[2]
    spec = json.loads(
        (repo_root / "backend/evals/run_specs/dual-entry-builder-http-slice.json")
        .read_text(encoding="utf-8")
    )
    restart = spec["recovery"]["backend_yjs_restart"]

    assert restart["command_id"] == "frontend-dual-user-backend-yjs-restart-v3-nine-case-matrix"
    assert restart["required_case_count"] == len(restart["case_ids"]) == 9
    assert len(set(restart["case_ids"])) == 9
    assert {case_id.split(".")[1] for case_id in restart["case_ids"]} == {"bj", "sh", "hz"}
    assert restart["direct_domain_calls"] == restart["sql_seed_operations"] == 0
