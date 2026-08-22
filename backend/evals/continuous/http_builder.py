from __future__ import annotations

import copy
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier
from typing import Any, Callable, Mapping

from evals.dual_entry_scorer import (
    aggregate_metric_scores,
    builder_metric_actuals,
    evaluate_metric_thresholds,
    score_metric_oracles,
)
from evals.frozen_suggestion_oracle import (
    canonical_builder_actuals,
    load_bound_oracle,
    overlay_case_oracles,
)

from .core import RunResult, _atomic_write_json, _canonical_bytes, _new_run_dir, _sha256_bytes, preflight
from .http_import import (
    HttpTransport,
    UrllibTransport,
    _Recorder,
    _atomic_write_jsonl,
    _check_receipt,
    _detail_code,
    _expect,
    _load_rows,
)
from .restart_gate import RestartGateResult, run_checked_in_restart_gate


_SUPPORTED_STEPS = {
    "create_workspace",
    "search_seed",
    "accept_seed",
    "request_suggestions",
    "preview_candidate",
    "dismiss_candidate",
    "accept_candidate",
    "undo",
    "readback",
    "concurrent_edit",
    "drag_stop",
    "move_stop_button",
    "incremental_audit",
}
_NO_PUBLIC_HTTP_ENDPOINT = {
    "restart_backend_yjs": "BACKEND_YJS_RESTART_GATE_NOT_EXECUTED_OR_FAILED",
}
_OUTSIDE_SLICE: dict[str, str] = {}
_CITY_COORDS = {
    "北京": {"lng": 116.397, "lat": 39.916},
    "上海": {"lng": 121.490, "lat": 31.241},
    "杭州": {"lng": 120.148, "lat": 30.248},
}


def _frozen_snapshot_anchors(
    resolved_spec: Mapping[str, Any],
    repo_root: Path,
) -> dict[str, dict[str, Any]]:
    provider = resolved_spec.get("provider", {})
    if provider.get("mode") != "frozen_snapshot":
        return {}
    snapshot_path = provider.get("snapshot_path")
    if not isinstance(snapshot_path, str) or not snapshot_path:
        raise ValueError("FROZEN_SNAPSHOT_PATH_REQUIRED_FOR_BUILDER_BOOTSTRAP")
    payload = json.loads((repo_root / snapshot_path).read_text(encoding="utf-8"))
    chained = payload.get("schema_version") == "1.1"
    anchors: dict[str, dict[str, Any]] = {}
    for row in payload.get("cities", []):
        anchor_key = "initial_anchor" if "initial_anchor" in row else "anchor"
        if not isinstance(row, dict) or not isinstance(row.get(anchor_key), dict):
            raise ValueError("FROZEN_SNAPSHOT_ANCHOR_INVALID")
        city = str(row.get("city") or "")
        anchor = row[anchor_key]
        if (
            not city
            or anchor.get("authority") != "fixed_canonical_anchor"
            or not isinstance(anchor.get("place_id"), str)
            or not isinstance(anchor.get("name"), str)
            or not isinstance(anchor.get("coords"), dict)
        ):
            raise ValueError("FROZEN_SNAPSHOT_ANCHOR_INVALID")
        resolved = copy.deepcopy(anchor)
        if chained:
            selected_chain = row.get("selected_chain_place_ids")
            if (
                not isinstance(selected_chain, list)
                or len(selected_chain) != 4
                or len(set(selected_chain)) != 4
                or selected_chain[0] != anchor["place_id"]
                or any(not isinstance(item, str) or not item for item in selected_chain)
            ):
                raise ValueError("FROZEN_SNAPSHOT_SELECTED_CHAIN_INVALID")
            resolved["selected_chain_place_ids"] = list(selected_chain)
        anchors[city] = resolved
    return anchors


def _verify_product_provider_handshake(
    recorder: _Recorder,
    provider: Mapping[str, Any],
) -> dict[str, Any]:
    response = recorder.request("__run__", "suggestion_provider_handshake", "GET", "/health")
    body = _expect(response, {200}, "suggestion_provider_handshake")
    actual = body.get("suggestion_provider")
    if not isinstance(actual, dict):
        raise RuntimeError("SUGGESTION_PROVIDER_HEALTH_IDENTITY_MISSING")
    expected_mode = provider.get("mode")
    if actual.get("mode") != expected_mode:
        raise RuntimeError("SUGGESTION_PROVIDER_MODE_MISMATCH")
    if expected_mode == "frozen_snapshot" and (
        actual.get("snapshot_id") != provider.get("snapshot_id")
        or actual.get("snapshot_sha256") != provider.get("snapshot_sha256")
        or not actual.get("replay_at")
    ):
        raise RuntimeError("SUGGESTION_FROZEN_SNAPSHOT_IDENTITY_MISMATCH")
    return actual


def _load_selected_builder_cases_and_labels(
    resolved_spec: Mapping[str, Any], repo_root: Path
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    dataset = resolved_spec["dataset"]
    if resolved_spec.get("lane") not in {"pr_offline", "nightly_snapshot"} or dataset.get(
        "label_access"
    ) != "development_scorer":
        raise ValueError("BUILDER_HTTP_ADAPTER_ONLY_SUPPORTS_DEVELOPMENT_LABELS")
    manifest_path = (repo_root / dataset["manifest"]).resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    requested = list(dataset.get("case_ids") or [])
    if not requested:
        raise ValueError("BUILDER_HTTP_ADAPTER_REQUIRES_EXPLICIT_CASE_IDS")
    cases: dict[str, dict[str, Any]] = {}
    labels: dict[str, dict[str, Any]] = {}
    entries = {entry["split"]: entry for entry in manifest["files"]}
    for split in dataset.get("splits", []):
        if split == "frozen_blind":
            raise ValueError("BLIND_LABEL_ACCESS_PROHIBITED")
        entry = entries[split]
        label_name = str(entry["labels"])
        if "sealed" in label_name.replace("\\", "/").split("/"):
            raise ValueError("BLIND_LABEL_ACCESS_PROHIBITED")
        for row in _load_rows((manifest_path.parent / entry["inputs"]).resolve()):
            if row.get("case_id") in requested:
                cases[row["case_id"]] = row
        for row in _load_rows((manifest_path.parent / label_name).resolve()):
            if row.get("case_id") in requested:
                labels[row["case_id"]] = row
    if any(case_id not in cases or case_id not in labels for case_id in requested):
        raise ValueError("SELECTED_BUILDER_CASE_OR_DEVELOPMENT_LABEL_MISSING")
    ordered = [cases[case_id] for case_id in requested]
    for case in ordered:
        if case.get("entry") != "BUILDER" or case.get("execution", {}).get("provider_mode") not in {
            "controlled_fixture",
            "frozen_snapshot",
        }:
            raise ValueError("BUILDER_HTTP_ADAPTER_CASE_SCOPE_MISMATCH")
    oracle_binding = dataset.get("graded_ranking_oracle")
    if oracle_binding is not None:
        if not isinstance(oracle_binding, dict) or oracle_binding.get("scope") != "development_only":
            raise ValueError("GRADED_RANKING_ORACLE_DEVELOPMENT_SCOPE_REQUIRED")
        artifact = load_bound_oracle(oracle_binding, repo_root)
        labels = {
            case["case_id"]: overlay_case_oracles(
                labels[case["case_id"]],
                artifact,
                city=str(case["city"]),
                requested_intents=(
                    case.get("input", {}).get("request_context", {}).get("intents")
                    or case.get("input", {}).get("intent")
                    or ["NEARBY", "POPULAR", "FUN", "FOOD"]
                ),
            )
            for case in ordered
        }
    return ordered, labels


def _seed_itinerary(
    case: Mapping[str, Any],
    workspace_id: str,
    suffix: str,
    *,
    provider_anchor: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    seed = dict(case["input"]["seed"])
    if provider_anchor is not None:
        seed.update(
            {
                "place_id": provider_anchor["place_id"],
                "name": provider_anchor["name"],
                "coords": provider_anchor["coords"],
            }
        )
    coords = seed.get("coords") or _CITY_COORDS.get(str(case["city"]))
    if not isinstance(coords, dict) or not {"lng", "lat"}.issubset(coords):
        raise ValueError("BUILDER_PUBLIC_SEED_COORDINATES_UNAVAILABLE")
    start = date(2026, 10, 1)
    seed_day = int(seed.get("day_index", 0))
    days = []
    for day_index in range(int(case["trip_days"])):
        slots: list[dict[str, Any]] = []
        if day_index == seed_day:
            slots.append(
                {
                    "place_id": seed["place_id"],
                    "place": {
                        "place_id": seed["place_id"],
                        "name": seed["name"],
                        "category": str(seed.get("category", "attraction")).casefold(),
                        "address": "controlled public-HTTP evaluation seed",
                        "coords": coords,
                        "city": case["city"],
                        "source": "synthesized",
                        "description": "controlled evaluation seed; not provider evidence",
                    },
                    "start_time": seed.get("start_time", "09:00"),
                    "end_time": seed.get("end_time", "10:00"),
                    "transport": None,
                    "tips": [],
                }
            )
        days.append(
            {
                "day_index": day_index,
                "date": (start + timedelta(days=day_index)).isoformat(),
                "cluster_id": day_index,
                "slots": slots,
                "weather_summary": None,
            }
        )
    return {
        "itinerary_id": f"eval-builder-itinerary-{suffix}",
        "thread_id": f"eval-builder-thread-{suffix}",
        "city": case["city"],
        "days": days,
        "generated_at": "2026-08-21T00:00:00+00:00",
        "version": 1,
    }


def _find_seed_stop(snapshot: Mapping[str, Any], seed_place_id: str) -> str:
    revision = snapshot.get("current_revision")
    if not isinstance(revision, dict) or revision.get("revision") != 1:
        raise RuntimeError("public workspace seed did not create revision 1")
    for day in revision.get("days", []):
        for stop in day.get("stops", []):
            if stop.get("place_id") == seed_place_id and isinstance(stop.get("stop_id"), str):
                return stop["stop_id"]
    raise RuntimeError("seed stop is absent from revision 1 readback")


def _frozen_hash(suggestion_set: Mapping[str, Any]) -> str:
    return _sha256_bytes(_canonical_bytes(suggestion_set))


def _candidate_usable(candidate: Mapping[str, Any], city: str) -> bool:
    place = candidate.get("canonical_place")
    hard_gate = candidate.get("hard_gate")
    freshness = candidate.get("evidence_freshness")
    route_delta = candidate.get("route_delta")
    return bool(
        isinstance(place, dict)
        and place.get("city") == city
        and isinstance(hard_gate, dict)
        and hard_gate.get("passed") is True
        and isinstance(freshness, dict)
        and freshness.get("status") == "FRESH"
        and isinstance(route_delta, dict)
        and route_delta.get("status") == "AVAILABLE"
    )


def _collect_suggestion_receipts(
    case_id: str, suggestion_set: Mapping[str, Any], round_index: int
) -> list[dict[str, Any]]:
    rows = []
    for candidate in suggestion_set.get("candidates", []):
        if not isinstance(candidate, dict) or not isinstance(candidate.get("provider_receipt"), dict):
            continue
        rows.append(
            {
                "schema_version": "continuous-provider-receipt-v1",
                "case_id": case_id,
                "phase": "suggestion_set",
                "round_index": round_index,
                "suggestion_set_id": suggestion_set.get("suggestion_set_id"),
                "candidate_id": candidate.get("candidate_id"),
                "rank_position": candidate.get("rank_position"),
                "receipt": candidate["provider_receipt"],
            }
        )
    return rows


def _unsupported_capabilities(
    case: Mapping[str, Any], *, restart_gate_passed: bool = False
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for step in case.get("execution", {}).get("steps", []):
        if step == "restart_backend_yjs" and restart_gate_passed:
            continue
        if step in _NO_PUBLIC_HTTP_ENDPOINT:
            rows.append({"step": step, "status": "UNSUPPORTED", "reason": _NO_PUBLIC_HTTP_ENDPOINT[step]})
        elif step in _OUTSIDE_SLICE:
            rows.append({"step": step, "status": "UNSUPPORTED", "reason": _OUTSIDE_SLICE[step]})
        elif step not in _SUPPORTED_STEPS:
            rows.append({"step": step, "status": "UNSUPPORTED", "reason": "STEP_NOT_IN_BUILDER_HTTP_SLICE"})
    unique: dict[str, dict[str, str]] = {}
    for row in rows:
        unique.setdefault(row["step"], row)
    return list(unique.values())


def _event_has_correlation(event: Mapping[str, Any]) -> bool:
    return all(event.get(key) not in {None, ""} for key in ("event_id", "session_id", "workspace_id", "actor_id", "occurred_at"))


def _interaction_plan(case: Mapping[str, Any]) -> tuple[dict[int, list[dict[str, str]]], bool]:
    """Map declarative interaction actions to their displayed SuggestionSet round."""
    by_round: dict[int, list[dict[str, str]]] = {}
    round_index = 0
    line_completed = False
    for raw_action in case.get("input", {}).get("action_sequence", []):
        action = str(raw_action)
        if action.startswith("show:"):
            round_index += 1
        elif action.startswith("preview:"):
            by_round.setdefault(max(round_index, 1), []).append(
                {"event_type": "candidate_previewed", "target": action.split(":", 1)[1]}
            )
        elif action.startswith("dismiss:"):
            parts = action.split(":", 2)
            if len(parts) != 3 or not parts[2].strip():
                raise ValueError("BUILDER_DISMISS_ACTION_REQUIRES_REASON")
            by_round.setdefault(max(round_index, 1), []).append(
                {
                    "event_type": "candidate_dismissed",
                    "target": parts[1],
                    "reason_code": parts[2],
                }
            )
        elif action == "line_completed":
            line_completed = True
    return by_round, line_completed


def _interaction_candidate(
    candidates: list[dict[str, Any]],
    target: str,
    *,
    avoid: set[str],
    protected_place_ids: set[str] | None = None,
) -> dict[str, Any] | None:
    protected = protected_place_ids or set()
    eligible = [
        candidate
        for candidate in candidates
        if str(candidate.get("candidate_id")) not in avoid
        and str((candidate.get("canonical_place") or {}).get("place_id")) not in protected
    ]
    for candidate in eligible:
        place = candidate.get("canonical_place") if isinstance(candidate.get("canonical_place"), dict) else {}
        receipt = candidate.get("provider_receipt") if isinstance(candidate.get("provider_receipt"), dict) else {}
        identities = {
            candidate.get("candidate_id"),
            place.get("place_id"),
            receipt.get("provider_place_id"),
        }
        if target in identities:
            return candidate
    return eligible[0] if eligible else None


def _captured_chain_accept_place_id(
    provider_anchor: Mapping[str, Any] | None,
    round_index: int,
) -> str | None:
    if provider_anchor is None or "selected_chain_place_ids" not in provider_anchor:
        return None
    selected_chain = provider_anchor.get("selected_chain_place_ids")
    if not isinstance(selected_chain, list) or len(selected_chain) != 4:
        raise ValueError("FROZEN_SNAPSHOT_SELECTED_CHAIN_INVALID")
    if not 1 <= round_index < len(selected_chain):
        raise ValueError("FROZEN_SNAPSHOT_SELECTED_CHAIN_ROUND_INVALID")
    value = selected_chain[round_index]
    if not isinstance(value, str) or not value:
        raise ValueError("FROZEN_SNAPSHOT_SELECTED_CHAIN_INVALID")
    return value


def _event_matches_frozen_context(
    event: Mapping[str, Any],
    suggestion_set: Mapping[str, Any],
    *,
    workspace_id: str,
    current_revision: int,
    event_type: str,
    candidate: Mapping[str, Any] | None,
    reason_code: str | None,
) -> bool:
    expected_candidate_id = candidate.get("candidate_id") if candidate else None
    expected_rank = candidate.get("rank_position") if candidate else None
    return bool(
        _event_has_correlation(event)
        and event.get("workspace_id") == workspace_id
        and event.get("session_id") == suggestion_set.get("session_id")
        and event.get("event_type") == event_type
        and event.get("revision_before") == current_revision
        and event.get("revision_after") is None
        and event.get("suggestion_set_id") == suggestion_set.get("suggestion_set_id")
        and event.get("candidate_id") == expected_candidate_id
        and event.get("rank_position") == expected_rank
        and event.get("context_hash") == suggestion_set.get("context_hash")
        and event.get("policy_version") == suggestion_set.get("policy_version")
        and event.get("provider_snapshot_id") == suggestion_set.get("provider_snapshot_id")
        and event.get("reason_code") == reason_code
    )


def _run_interaction_command(
    recorder: _Recorder,
    *,
    case_id: str,
    workspace_id: str,
    suggestion_set: Mapping[str, Any],
    candidate: Mapping[str, Any] | None,
    event_type: str,
    reason_code: str | None,
    current_revision: int,
    round_index: int,
    command_index: int,
    bearer_token: str,
) -> tuple[dict[str, Any] | None, str | None]:
    set_id = str(suggestion_set["suggestion_set_id"])
    candidate_id = str(candidate["candidate_id"]) if candidate else None
    suffix = {
        "candidate_previewed": "preview",
        "candidate_dismissed": "dismiss",
        "line_completed": "line-completed",
    }[event_type]
    if event_type == "line_completed":
        path = f"/api/trip-workspaces/{workspace_id}/suggestion-sets/{set_id}:line-completed"
    else:
        path = (
            f"/api/trip-workspaces/{workspace_id}/suggestion-sets/{set_id}"
            f"/candidates/{candidate_id}:{suffix}"
        )
    key = f"continuous-builder-{suffix}-{_sha256_bytes(case_id.encode())[:16]}-{round_index}-{command_index}"
    body = {"reason_code": reason_code} if event_type == "candidate_dismissed" else None
    step = f"{suffix}_candidate_{round_index}_{command_index}" if candidate else f"line_completed_{round_index}"
    response = recorder.request(
        case_id,
        step,
        "POST",
        path,
        bearer_token=bearer_token,
        headers={"Idempotency-Key": key},
        json_body=body,
    )
    if response.status_code != 200 or not isinstance(response.body, dict):
        return None, _detail_code(response) or f"HTTP_{response.status_code}"
    result = response.body
    event = result.get("event")
    if (
        result.get("idempotent_replay") is not False
        or not isinstance(event, dict)
        or not _event_matches_frozen_context(
            event,
            suggestion_set,
            workspace_id=workspace_id,
            current_revision=current_revision,
            event_type=event_type,
            candidate=candidate,
            reason_code=reason_code,
        )
    ):
        return None, "EVENT_COMMAND_FROZEN_CONTEXT_INVALID"
    replay = recorder.request(
        case_id,
        f"replay_{step}",
        "POST",
        path,
        bearer_token=bearer_token,
        headers={"Idempotency-Key": key},
        json_body=body,
    )
    if (
        replay.status_code != 200
        or not isinstance(replay.body, dict)
        or replay.body.get("idempotent_replay") is not True
        or replay.body.get("event") != event
    ):
        return None, "EVENT_COMMAND_IDEMPOTENT_REPLAY_FAILED"
    return {
        "event_type": event_type,
        "suggestion_set_id": set_id,
        "candidate_id": candidate_id,
        "reason_code": reason_code,
        "event": event,
        "frozen_context_valid": True,
        "idempotent_replay": True,
    }, None


def _expected_rounds(case: Mapping[str, Any]) -> int:
    accept_actions = sum(
        str(action).startswith("accept:") for action in case.get("input", {}).get("action_sequence", [])
    )
    return max(3 if "four-stop" in case.get("tags", []) else 1, accept_actions)


def _required_steps(case: Mapping[str, Any]) -> set[str]:
    return {str(step) for step in case.get("execution", {}).get("steps", [])}


def _finding_semantics(finding: Mapping[str, Any]) -> dict[str, Any]:
    """Drop only run-specific evidence handles before incremental/full comparison."""

    return {
        key: copy.deepcopy(value)
        for key, value in finding.items()
        if key not in {"finding_id", "evidence_fact_ids"}
    }


def _replace_logical_stop_identity(value: Any, *, physical_stop_id: str) -> Any:
    """Normalize one workspace-local stop identity for cross-workspace comparison."""

    if isinstance(value, str):
        return "$SEED_STOP" if value == physical_stop_id else value
    if isinstance(value, list):
        return [
            _replace_logical_stop_identity(item, physical_stop_id=physical_stop_id)
            for item in value
        ]
    if isinstance(value, dict):
        return {
            key: _replace_logical_stop_identity(item, physical_stop_id=physical_stop_id)
            for key, item in value.items()
        }
    return value


def _normalized_revision_semantic_hash(
    revision: Mapping[str, Any], *, physical_stop_id: str
) -> str:
    """Mirror the product content-hash payload while normalizing local IDs.

    Separate workspaces must not reuse a globally unique itinerary identity just
    to manufacture equal stop UUIDs.  UI-path equivalence is therefore measured
    over the same semantic fields as the product content hash, with the one
    corresponding seed-stop identity replaced by a logical placeholder.
    """

    days: list[dict[str, Any]] = []
    for day in sorted(revision.get("days", []), key=lambda item: int(item["day_index"])):
        stops: list[dict[str, Any]] = []
        for stop in sorted(day.get("stops", []), key=lambda item: int(item["order_index"])):
            transport = stop.get("transport_to_next")
            stops.append(
                {
                    "stop_id": stop.get("stop_id"),
                    "place_id": stop.get("place_id"),
                    "order_index": stop.get("order_index"),
                    "start_time": stop.get("start_time"),
                    "end_time": stop.get("end_time"),
                    "visit_duration_minutes": stop.get("visit_duration_minutes"),
                    "transport_mode": transport.get("mode") if isinstance(transport, dict) else None,
                    "locked": stop.get("locked"),
                    "commitment_kind": stop.get("commitment_kind"),
                    "fixed_commitment": stop.get("fixed_commitment"),
                }
            )
        days.append({"day_index": day.get("day_index"), "date": day.get("date"), "stops": stops})
    payload = {
        "city": revision.get("city"),
        "date_range": revision.get("date_range"),
        "days": days,
    }
    normalized = _replace_logical_stop_identity(
        payload,
        physical_stop_id=physical_stop_id,
    )
    return _sha256_bytes(_canonical_bytes(normalized))


def _create_recovery_workspace(
    recorder: _Recorder,
    *,
    case: Mapping[str, Any],
    case_id: str,
    bearer_token: str,
    workspace_id: str,
    room_id: str,
    semantic_seed_suffix: str,
    provider_anchor: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], str]:
    start = date(2026, 10, 1)
    end = start + timedelta(days=int(case["trip_days"]) - 1)
    _expect(
        recorder.request(
            case_id,
            f"recovery_create_room_{workspace_id.rsplit('-', 1)[-1]}",
            "POST",
            "/api/room",
            bearer_token=bearer_token,
            json_body={
                "room_id": room_id,
                "thread_id": f"{room_id}-thread",
                "trip_city": case["city"],
                "trip_days": case["trip_days"],
                "nickname": "Continuous Builder Recovery Eval",
            },
        ),
        {200},
        "recovery_create_room",
    )
    _expect(
        recorder.request(
            case_id,
            f"recovery_create_workspace_{workspace_id.rsplit('-', 1)[-1]}",
            "POST",
            "/api/trip-workspaces",
            bearer_token=bearer_token,
            json_body={
                "workspace_id": workspace_id,
                "room_id": room_id,
                "city": case["city"],
                "trip_date_range": {"start": start.isoformat(), "end": end.isoformat()},
                # Each workspace owns a globally distinct itinerary identity.
                # UI equivalence is compared later with its corresponding stop
                # identity normalized to the same logical placeholder.
                "initial_itinerary": _seed_itinerary(
                    case,
                    workspace_id,
                    semantic_seed_suffix,
                    provider_anchor=provider_anchor,
                ),
            },
        ),
        {201},
        "recovery_create_workspace",
    )
    snapshot = _expect(
        recorder.request(
            case_id,
            f"recovery_seed_readback_{workspace_id.rsplit('-', 1)[-1]}",
            "GET",
            f"/api/trip-workspaces/{workspace_id}/snapshot",
            bearer_token=bearer_token,
        ),
        {200},
        "recovery_seed_readback",
    )
    seed_place_id = (
        str(provider_anchor["place_id"])
        if provider_anchor is not None
        else str(case["input"]["seed"]["place_id"])
    )
    return snapshot, _find_seed_stop(snapshot, seed_place_id)


def _post_public_edit(
    recorder: _Recorder,
    *,
    case_id: str,
    step: str,
    workspace_id: str,
    bearer_token: str,
    command_id: str,
    base_revision: int,
    operation: str,
    payload: Mapping[str, Any],
) -> Any:
    return recorder.request(
        case_id,
        step,
        "POST",
        f"/api/trip-workspaces/{workspace_id}/edits",
        bearer_token=bearer_token,
        headers={"If-Match": f'"{base_revision}"', "Idempotency-Key": command_id},
        json_body={
            "command_id": command_id,
            "base_revision": base_revision,
            "operation": operation,
            "payload": dict(payload),
        },
    )


def _full_audit_parity(
    recorder: _Recorder,
    *,
    case_id: str,
    workspace_id: str,
    bearer_token: str,
    edit_result: Mapping[str, Any],
    suffix: str,
) -> dict[str, Any]:
    audit_response = recorder.request(
        case_id,
        f"full_audit_after_{suffix}",
        "POST",
        f"/api/trip-workspaces/{workspace_id}/audits",
        bearer_token=bearer_token,
        headers={"Idempotency-Key": f"continuous-builder-full-audit-{suffix}"},
        json_body={},
    )
    if audit_response.status_code != 200 or not isinstance(audit_response.body, dict):
        return {
            "status": "FAIL",
            "reason_code": _detail_code(audit_response) or f"HTTP_{audit_response.status_code}",
        }
    affected_rule_ids = set(edit_result.get("affected_rule_ids") or [])
    changed_days = set(edit_result.get("changed_days") or [])
    full_affected = [
        finding
        for finding in audit_response.body.get("findings", [])
        if isinstance(finding, dict)
        and finding.get("rule_id") in affected_rule_ids
        and (
            not finding.get("affected_days")
            or changed_days.intersection(finding.get("affected_days") or [])
        )
    ]
    incremental = [
        _finding_semantics(finding)
        for finding in edit_result.get("incremental_findings", [])
        if isinstance(finding, dict)
    ]
    full = [_finding_semantics(finding) for finding in full_affected]
    return {
        "status": "PASS" if incremental == full else "FAIL",
        "audit_mode": edit_result.get("audit_mode"),
        "affected_rule_ids": sorted(affected_rule_ids),
        "changed_days": sorted(changed_days),
        "incremental_finding_semantics": incremental,
        "full_affected_finding_semantics": full,
        "full_report_id": audit_response.body.get("report_id"),
        "full_report_revision": audit_response.body.get("itinerary_revision"),
    }


def _run_drag_button_equivalence(
    recorder: _Recorder,
    *,
    case: Mapping[str, Any],
    bearer_token: str,
    provider_anchor: Mapping[str, Any] | None,
    run_namespace: str,
) -> dict[str, Any]:
    case_id = str(case["case_id"])
    suffix = _sha256_bytes(f"{run_namespace}:{case_id}:move-equivalence".encode())[:16]
    sides: dict[str, dict[str, Any]] = {}
    seed_snapshots: dict[str, dict[str, Any]] = {}
    stop_ids: dict[str, str] = {}
    for origin in ("drag", "button"):
        workspace_id = f"eval-builder-recovery-{suffix}-{origin}"
        snapshot, stop_id = _create_recovery_workspace(
            recorder,
            case=case,
            case_id=case_id,
            bearer_token=bearer_token,
            workspace_id=workspace_id,
            room_id=f"eval-builder-recovery-room-{suffix}-{origin}",
            semantic_seed_suffix=f"{suffix}-{origin}",
            provider_anchor=provider_anchor,
        )
        seed_snapshots[origin] = snapshot
        stop_ids[origin] = stop_id
        sides[origin] = {"workspace_id": workspace_id}
    seed_day = int(case.get("input", {}).get("seed", {}).get("day_index", 0))
    target_day = 1 if seed_day == 0 else 0
    semantic_command = {
        "base_revision": 1,
        "operation": "MOVE_TO_DAY",
        "payload": {
            "stop_id": "$SEED_STOP",
            "target_day_index": target_day,
            "target_order_index": 0,
        },
    }
    for origin in ("drag", "button"):
        command_id = f"continuous-builder-{origin}-move-{suffix}"
        response = _post_public_edit(
            recorder,
            case_id=case_id,
            step=f"{origin}_move_public_edit",
            workspace_id=sides[origin]["workspace_id"],
            bearer_token=bearer_token,
            command_id=command_id,
            base_revision=1,
            operation=semantic_command["operation"],
            payload={**semantic_command["payload"], "stop_id": stop_ids[origin]},
        )
        if response.status_code != 200 or not isinstance(response.body, dict):
            return {
                "status": "FAIL",
                "reason_code": _detail_code(response) or f"{origin.upper()}_HTTP_{response.status_code}",
            }
        revision = _expect(
            recorder.request(
                case_id,
                f"{origin}_move_revision_readback",
                "GET",
                f"/api/trip-workspaces/{sides[origin]['workspace_id']}/revisions/2",
                bearer_token=bearer_token,
            ),
            {200},
            f"{origin}_move_revision_readback",
        )
        sides[origin].update({"edit_result": response.body, "revision": revision})
        sides[origin]["incremental_full_parity"] = _full_audit_parity(
            recorder,
            case_id=case_id,
            workspace_id=sides[origin]["workspace_id"],
            bearer_token=bearer_token,
            edit_result=response.body,
            suffix=f"{suffix}-{origin}",
        )
        stale = _post_public_edit(
            recorder,
            case_id=case_id,
            step=f"{origin}_stale_move_failure",
            workspace_id=sides[origin]["workspace_id"],
            bearer_token=bearer_token,
            command_id=f"continuous-builder-{origin}-stale-{suffix}",
            base_revision=1,
            operation=semantic_command["operation"],
            payload={**semantic_command["payload"], "stop_id": stop_ids[origin]},
        )
        rollback = _expect(
            recorder.request(
                case_id,
                f"{origin}_failure_rollback_readback",
                "GET",
                f"/api/trip-workspaces/{sides[origin]['workspace_id']}/snapshot",
                bearer_token=bearer_token,
            ),
            {200},
            f"{origin}_failure_rollback_readback",
        )
        sides[origin].update(
            {
                "failure_status_code": stale.status_code,
                "failure_code": _detail_code(stale),
                "rollback_revision": (rollback.get("current_revision") or {}).get("revision"),
                "rollback_content_hash": (rollback.get("current_revision") or {}).get("content_hash"),
            }
        )
    drag_result = sides["drag"]["edit_result"]
    button_result = sides["button"]["edit_result"]
    result_fields = ("changed_days", "changed_route_edges", "affected_rule_ids", "route_delta")
    outputs_equivalent = all(
        _replace_logical_stop_identity(
            drag_result.get(field), physical_stop_id=stop_ids["drag"]
        )
        == _replace_logical_stop_identity(
            button_result.get(field), physical_stop_id=stop_ids["button"]
        )
        for field in result_fields
    )
    normalized_revision_hashes = {
        origin: _normalized_revision_semantic_hash(
            sides[origin]["revision"], physical_stop_id=stop_ids[origin]
        )
        for origin in ("drag", "button")
    }
    normalized_revision_hash_equal = (
        normalized_revision_hashes["drag"] == normalized_revision_hashes["button"]
    )
    raw_revision_hash_equal = sides["drag"]["revision"].get("content_hash") == sides[
        "button"
    ]["revision"].get("content_hash")
    rollback_equivalent = bool(
        sides["drag"]["failure_status_code"] == sides["button"]["failure_status_code"] == 409
        and sides["drag"]["failure_code"]
        == sides["button"]["failure_code"]
        == "ITINERARY_REVISION_CONFLICT"
        and sides["drag"]["rollback_revision"] == sides["button"]["rollback_revision"] == 2
        and all(
            sides[origin]["rollback_content_hash"]
            == sides[origin]["revision"].get("content_hash")
            for origin in ("drag", "button")
        )
    )
    incremental_parity = all(
        sides[origin]["incremental_full_parity"].get("status") == "PASS"
        for origin in ("drag", "button")
    )
    passed = (
        outputs_equivalent
        and normalized_revision_hash_equal
        and rollback_equivalent
        and incremental_parity
    )
    return {
        "status": "PASS" if passed else "FAIL",
        "transport": "PUBLIC_HTTP_ONLY",
        "semantic_command": semantic_command,
        "semantic_command_hash": _sha256_bytes(_canonical_bytes(semantic_command)),
        "outputs_equivalent": outputs_equivalent,
        "normalized_revision_semantic_hash_equal": normalized_revision_hash_equal,
        "normalized_revision_semantic_hashes": normalized_revision_hashes,
        "raw_revision_content_hash_equal": raw_revision_hash_equal,
        "raw_hash_comparison_scope": "WORKSPACE_LOCAL_IDENTITY_NOT_CROSS_WORKSPACE_SEMANTICS",
        "failure_rollback_equivalent": rollback_equivalent,
        "incremental_full_audit_semantic_parity": incremental_parity,
        "sides": sides,
    }


def _run_concurrent_edit_contract(
    recorder: _Recorder,
    *,
    case: Mapping[str, Any],
    bearer_token: str,
    provider_anchor: Mapping[str, Any] | None,
    run_namespace: str,
) -> dict[str, Any]:
    case_id = str(case["case_id"])
    suffix = _sha256_bytes(f"{run_namespace}:{case_id}:concurrency".encode())[:16]
    workspace_id = f"eval-builder-concurrency-{suffix}"
    snapshot, stop_id = _create_recovery_workspace(
        recorder,
        case=case,
        case_id=case_id,
        bearer_token=bearer_token,
        workspace_id=workspace_id,
        room_id=f"eval-builder-concurrency-room-{suffix}",
        semantic_seed_suffix=f"{suffix}-seed",
        provider_anchor=provider_anchor,
    )
    initial_hash = (snapshot.get("current_revision") or {}).get("content_hash")
    barrier = Barrier(2)

    def write(client: str):
        barrier.wait(timeout=5)
        return client, _post_public_edit(
            recorder,
            case_id=case_id,
            step=f"concurrent_client_{client}_edit",
            workspace_id=workspace_id,
            bearer_token=bearer_token,
            command_id=f"continuous-builder-concurrent-{suffix}-{client}",
            base_revision=1,
            operation="LOCK_STOP",
            payload={"stop_id": stop_id},
        )

    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="builder-concurrent-edit") as pool:
        pairs = list(pool.map(write, ("a", "b")))
    responses = {client: response for client, response in pairs}
    winners = [client for client, response in responses.items() if response.status_code == 200]
    losers = [
        client
        for client, response in responses.items()
        if response.status_code == 409 and _detail_code(response) == "ITINERARY_REVISION_CONFLICT"
    ]
    loser_reload = None
    if len(losers) == 1:
        loser_reload = recorder.request(
            case_id,
            f"concurrent_client_{losers[0]}_explicit_reload",
            "GET",
            f"/api/trip-workspaces/{workspace_id}/snapshot",
            bearer_token=bearer_token,
        )
    reload_body = loser_reload.body if loser_reload and isinstance(loser_reload.body, dict) else {}
    current = reload_body.get("current_revision") if isinstance(reload_body, dict) else None
    passed = bool(
        len(winners) == len(losers) == 1
        and loser_reload is not None
        and loser_reload.status_code == 200
        and isinstance(current, dict)
        and current.get("revision") == 2
        and current.get("content_hash") != initial_hash
    )
    return {
        "status": "PASS" if passed else "FAIL",
        "transport": "PUBLIC_HTTP_ONLY",
        "base_revision": 1,
        "client_statuses": {
            client: {
                "status_code": response.status_code,
                "detail_code": _detail_code(response),
                "new_revision": response.body.get("new_revision") if isinstance(response.body, dict) else None,
            }
            for client, response in responses.items()
        },
        "winner_client": winners[0] if len(winners) == 1 else None,
        "loser_client": losers[0] if len(losers) == 1 else None,
        "loser_explicit_reload": bool(loser_reload and loser_reload.status_code == 200),
        "reloaded_revision": current.get("revision") if isinstance(current, dict) else None,
        "reloaded_content_hash": current.get("content_hash") if isinstance(current, dict) else None,
    }


def _run_recovery_contracts(
    recorder: _Recorder,
    *,
    case: Mapping[str, Any],
    bearer_token: str,
    provider_anchor: Mapping[str, Any] | None,
    run_namespace: str,
    restart_gate: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    steps = _required_steps(case)
    result: dict[str, Any] = {}
    if steps.intersection({"drag_stop", "move_stop_button", "incremental_audit"}):
        try:
            result["drag_button_equivalence"] = _run_drag_button_equivalence(
                recorder,
                case=case,
                bearer_token=bearer_token,
                provider_anchor=provider_anchor,
                run_namespace=run_namespace,
            )
        except Exception as exc:
            result["drag_button_equivalence"] = {
                "status": "FAIL",
                "reason_code": "PUBLIC_RECOVERY_WORKFLOW_FAILED",
                "message": str(exc),
            }
    if "concurrent_edit" in steps:
        try:
            result["concurrent_edit"] = _run_concurrent_edit_contract(
                recorder,
                case=case,
                bearer_token=bearer_token,
                provider_anchor=provider_anchor,
                run_namespace=run_namespace,
            )
        except Exception as exc:
            result["concurrent_edit"] = {
                "status": "FAIL",
                "reason_code": "PUBLIC_CONCURRENCY_WORKFLOW_FAILED",
                "message": str(exc),
            }
    if "restart_backend_yjs" in steps and restart_gate is not None:
        result["backend_yjs_restart"] = dict(restart_gate)
    return result


def _score_case(
    case: Mapping[str, Any],
    label: Mapping[str, Any],
    output: Mapping[str, Any],
    receipts: list[dict[str, Any]],
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    rounds = output.get("rounds", [])
    checks: list[dict[str, Any]] = []
    expected_rounds = _expected_rounds(case)
    checks.append(
        {
            "id": "SUGGESTION_ROUNDS_COMPLETED",
            "status": "PASS" if len(rounds) == expected_rounds else "FAIL",
            "expected": expected_rounds,
            "actual": len(rounds),
        }
    )
    for index, item in enumerate(rounds, start=1):
        candidates = item.get("suggestion_set", {}).get("candidates", [])
        usable_top3 = all(_candidate_usable(candidate, str(case["city"])) for candidate in candidates[:3])
        checks.extend(
            [
                {
                    "id": f"ROUND_{index}_VISIBLE_CANDIDATES_4_TO_6",
                    "status": "PASS" if 4 <= len(candidates) <= 6 else "FAIL",
                    "actual": len(candidates),
                },
                {
                    "id": f"ROUND_{index}_TOP3_NO_HARD_WRONG_CITY_UNKNOWN_LEAK",
                    "status": "PASS" if len(candidates) >= 3 and usable_top3 else "FAIL",
                },
                {
                    "id": f"ROUND_{index}_FROZEN_READBACK_EXACT",
                    "status": "PASS" if item.get("create_hash") == item.get("readback_hash") else "FAIL",
                },
                {
                    "id": f"ROUND_{index}_ANCHOR_CONTEXT_EXACT",
                    "status": "PASS"
                    if item.get("suggestion_set", {}).get("insert_after_stop_id") == item.get("anchor_stop_id")
                    else "FAIL",
                },
                {
                    "id": f"ROUND_{index}_ACCEPT_ATOMIC_REVISION_INCREMENT",
                    "status": "PASS" if item.get("revision_after") == item.get("revision_before", 0) + 1 else "FAIL",
                },
            ]
        )
        expected_chain_place_id = item.get("expected_captured_chain_place_id")
        if expected_chain_place_id is not None:
            checks.append({
                "id": f"ROUND_{index}_CAPTURED_CHAIN_ACCEPT_EXACT",
                "status": (
                    "PASS"
                    if item.get("accepted_canonical_place_id") == expected_chain_place_id
                    else "FAIL"
                ),
                "expected": expected_chain_place_id,
                "actual": item.get("accepted_canonical_place_id"),
            })
    receipt_values = [row["receipt"] for row in receipts]
    checks.append(
        {
            "id": "SUGGESTION_PROVIDER_RECEIPTS_COMPLETE",
            "status": "PASS"
            if receipt_values and all(_check_receipt(item, case["execution"]["provider_mode"]) for item in receipt_values)
            else "FAIL",
            "receipt_count": len(receipt_values),
        }
    )
    accepted_events = [event for event in events if event.get("event_type") == "candidate_accepted"]
    shown_events = [event for event in events if event.get("event_type") == "suggestions_shown"]
    event_commands = output.get("event_commands", [])
    command_event_by_id = {
        item.get("event", {}).get("event_id"): item.get("event")
        for item in event_commands
        if isinstance(item, dict) and isinstance(item.get("event"), dict)
    }
    ledger_event_by_id = {event.get("event_id"): event for event in events}
    planned_interactions, line_completed = _interaction_plan(case)
    expected_command_count = sum(len(items) for items in planned_interactions.values()) + int(line_completed)
    checks.extend(
        [
            {
                "id": "EVENT_LEDGER_COUNTS",
                "status": "PASS" if len(accepted_events) >= len(rounds) and len(shown_events) >= len(rounds) else "FAIL",
                "shown": len(shown_events),
                "accepted": len(accepted_events),
            },
            {
                "id": "EVENT_CORRELATION_FIELDS",
                "status": "PASS" if events and all(_event_has_correlation(event) for event in events) else "FAIL",
            },
            {
                "id": "IDEMPOTENT_ACCEPT_REPLAY",
                "status": "PASS" if output.get("idempotency_replayed") is True else "FAIL",
            },
            {
                "id": "EVENT_COMMANDS_EXECUTED_OVER_PUBLIC_HTTP",
                "status": "PASS" if len(event_commands) == expected_command_count else "FAIL",
                "expected": expected_command_count,
                "actual": len(event_commands),
            },
            {
                "id": "EVENT_COMMANDS_FROZEN_CONTEXT_AND_IDEMPOTENCY",
                "status": "PASS"
                if all(
                    item.get("frozen_context_valid") is True and item.get("idempotent_replay") is True
                    for item in event_commands
                )
                else "FAIL",
            },
            {
                "id": "EVENT_COMMANDS_EXACT_LEDGER_READBACK",
                "status": "PASS"
                if all(ledger_event_by_id.get(event_id) == event for event_id, event in command_event_by_id.items())
                else "FAIL",
                "missing_event_ids": sorted(set(command_event_by_id) - set(ledger_event_by_id)),
                "mismatched_event_ids": sorted(
                    event_id
                    for event_id, event in command_event_by_id.items()
                    if event_id in ledger_event_by_id and ledger_event_by_id[event_id] != event
                ),
            },
            {
                "id": "NO_UNSUPPORTED_REQUIRED_CAPABILITY",
                "status": "PASS" if not output.get("unsupported_capabilities") else "FAIL",
                "unsupported": output.get("unsupported_capabilities", []),
            },
        ]
    )
    if output.get("undo_attempted"):
        undo_event = output.get("undo_event") or {}
        source_accept_event = output.get("undo_source_accept_event") or {}
        checks.extend(
            [
                {
                "id": "UNDO_APPEND_ONLY_REVISION",
                "status": "PASS" if output.get("undo_revision_after") == output.get("undo_revision_before", 0) + 1 else "FAIL",
                },
                {
                    "id": "UNDO_STOP_UNDONE_EVENT_READBACK",
                    "status": "PASS"
                    if (
                        undo_event.get("event_type") == "stop_undone"
                        and undo_event.get("revision_before") == output.get("undo_revision_before")
                        and undo_event.get("revision_after") == output.get("undo_revision_after")
                        and undo_event.get("reason_code") == "UNDO_ACCEPTED_SUGGESTION"
                        and undo_event.get("suggestion_set_id") == source_accept_event.get("suggestion_set_id")
                        and undo_event.get("candidate_id") == source_accept_event.get("candidate_id")
                        and (undo_event.get("payload") or {}).get("source_accept_event_id")
                        == source_accept_event.get("event_id")
                    )
                    else "FAIL",
                },
            ]
        )
    required_steps = _required_steps(case)
    recovery = output.get("recovery_contracts") or {}
    if required_steps.intersection({"drag_stop", "move_stop_button", "incremental_audit"}):
        equivalence = recovery.get("drag_button_equivalence") or {}
        checks.extend(
            [
                {
                    "id": "DRAG_BUTTON_PUBLIC_COMMAND_SEMANTIC_EQUIVALENCE",
                    "status": "PASS" if equivalence.get("status") == "PASS" else "FAIL",
                    "semantic_command_hash": equivalence.get("semantic_command_hash"),
                },
                {
                    "id": "DRAG_BUTTON_FAILURE_ROLLBACK_EQUIVALENCE",
                    "status": "PASS"
                    if equivalence.get("failure_rollback_equivalent") is True
                    else "FAIL",
                },
            ]
        )
    if "incremental_audit" in required_steps:
        equivalence = recovery.get("drag_button_equivalence") or {}
        checks.append(
            {
                "id": "INCREMENTAL_FULL_AUDIT_AFFECTED_SCOPE_SEMANTIC_PARITY",
                "status": "PASS"
                if equivalence.get("incremental_full_audit_semantic_parity") is True
                else "FAIL",
            }
        )
    if "concurrent_edit" in required_steps:
        concurrency = recovery.get("concurrent_edit") or {}
        checks.append(
            {
                "id": "CONCURRENT_SAME_BASE_SINGLE_WINNER_AND_LOSER_RELOAD",
                "status": "PASS" if concurrency.get("status") == "PASS" else "FAIL",
                "client_statuses": concurrency.get("client_statuses"),
                "loser_explicit_reload": concurrency.get("loser_explicit_reload"),
            }
        )
    if "restart_backend_yjs" in required_steps:
        restart = recovery.get("backend_yjs_restart") or {}
        checks.append(
            {
                "id": "BACKEND_YJS_PROCESS_RESTART_FRESH_READBACK",
                "status": "PASS" if restart.get("status") == "PASS" else "FAIL",
                "transport": restart.get("transport"),
                "revision_exact": restart.get("revision_exact"),
                "events_exact": restart.get("events_exact"),
                "restart_gate_reason_code": restart.get("restart_gate_reason_code"),
            }
        )
    if output.get("failed_step"):
        checks.append(
            {
                "id": "PRODUCT_HTTP_STEP_SUCCESS",
                "status": "FAIL",
                "step": output.get("failed_step"),
                "code": output.get("failure_code"),
            }
        )
    ndcg_oracle = label.get("metric_oracles", {}).get("builder_ndcg_at_5", {})
    actuals = (
        canonical_builder_actuals(output)
        if ndcg_oracle.get("identity_key") == "canonical_place.place_id"
        else builder_metric_actuals(output)
    )
    metric_score = score_metric_oracles(label, actuals)
    passed = all(check["status"] == "PASS" for check in checks) and metric_score["status"] == "SCORED"
    return {
        "schema_version": "continuous-deterministic-score-v1",
        "case_id": case["case_id"],
        "status": "PASS" if passed else "INVALID" if metric_score["status"] == "INVALID" else "FAIL",
        "checks": checks,
        "metric_score": metric_score,
    }


def _execute_case(
    recorder: _Recorder,
    case: Mapping[str, Any],
    bearer_token: str,
    *,
    provider_anchor: Mapping[str, Any] | None = None,
    run_namespace: str,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    case_id = str(case["case_id"])
    suffix = _sha256_bytes(f"{run_namespace}:{case_id}".encode("utf-8"))[:16]
    room_id = f"eval-builder-room-{suffix}"
    workspace_id = f"eval-builder-workspace-{suffix}"
    start = date(2026, 10, 1)
    end = start + timedelta(days=int(case["trip_days"]) - 1)
    _expect(
        recorder.request(
            case_id,
            "create_room",
            "POST",
            "/api/room",
            bearer_token=bearer_token,
            json_body={
                "room_id": room_id,
                "thread_id": f"eval-builder-thread-{suffix}",
                "trip_city": case["city"],
                "trip_days": case["trip_days"],
                "nickname": "Continuous Builder Eval",
            },
        ),
        {200},
        "create_room",
    )
    workspace = _expect(
        recorder.request(
            case_id,
            "create_workspace_with_seed_revision",
            "POST",
            "/api/trip-workspaces",
            bearer_token=bearer_token,
            json_body={
                "workspace_id": workspace_id,
                "room_id": room_id,
                "city": case["city"],
                "trip_date_range": {"start": start.isoformat(), "end": end.isoformat()},
                "initial_itinerary": _seed_itinerary(
                    case,
                    workspace_id,
                    suffix,
                    provider_anchor=provider_anchor,
                ),
            },
        ),
        {201},
        "create_workspace_with_seed_revision",
    )
    if workspace.get("current_itinerary_revision") != 1:
        raise RuntimeError("workspace seed response is not bound to revision 1")
    snapshot = _expect(
        recorder.request(
            case_id,
            "seed_revision_readback",
            "GET",
            f"/api/trip-workspaces/{workspace_id}/snapshot",
            bearer_token=bearer_token,
        ),
        {200},
        "seed_revision_readback",
    )
    current_revision = 1
    seed_place_id = (
        str(provider_anchor["place_id"])
        if provider_anchor is not None
        else str(case["input"]["seed"]["place_id"])
    )
    anchor_stop_id = _find_seed_stop(snapshot, seed_place_id)
    session_id = f"eval-builder-session-{suffix}"
    intents = case.get("input", {}).get("request_context", {}).get("intents") or case.get("input", {}).get(
        "intent"
    ) or ["NEARBY", "POPULAR", "FUN", "FOOD"]
    expected_rounds = _expected_rounds(case)
    rounds: list[dict[str, Any]] = []
    receipts: list[dict[str, Any]] = []
    failed_step: str | None = None
    failure_code: str | None = None
    idempotency_replayed = False
    event_commands: list[dict[str, Any]] = []
    interaction_plan, should_complete_line = _interaction_plan(case)
    last_frozen: dict[str, Any] | None = None
    for round_index in range(1, expected_rounds + 1):
        expected_accept_place_id = _captured_chain_accept_place_id(
            provider_anchor,
            round_index,
        )
        create_response = recorder.request(
            case_id,
            f"create_suggestion_set_{round_index}",
            "POST",
            f"/api/trip-workspaces/{workspace_id}/suggestion-sets",
            bearer_token=bearer_token,
            json_body={
                "base_revision": current_revision,
                "day_index": int(case["input"].get("seed", {}).get("day_index", 0)),
                "insert_after_stop_id": anchor_stop_id,
                "insert_before_stop_id": None,
                "intents": intents,
                "session_id": session_id,
            },
        )
        if create_response.status_code != 201 or not isinstance(create_response.body, dict):
            failed_step = f"create_suggestion_set_{round_index}"
            failure_code = _detail_code(create_response) or f"HTTP_{create_response.status_code}"
            break
        created = copy.deepcopy(create_response.body)
        set_id = created.get("suggestion_set_id")
        if not isinstance(set_id, str):
            failed_step = f"create_suggestion_set_{round_index}"
            failure_code = "SUGGESTION_SET_ID_MISSING"
            break
        get_response = recorder.request(
            case_id,
            f"read_frozen_suggestion_set_{round_index}",
            "GET",
            f"/api/trip-workspaces/{workspace_id}/suggestion-sets/{set_id}",
            bearer_token=bearer_token,
        )
        if get_response.status_code != 200 or not isinstance(get_response.body, dict):
            failed_step = f"read_frozen_suggestion_set_{round_index}"
            failure_code = _detail_code(get_response) or f"HTTP_{get_response.status_code}"
            break
        frozen = get_response.body
        last_frozen = frozen
        receipts.extend(_collect_suggestion_receipts(case_id, frozen, round_index))
        usable = [candidate for candidate in frozen.get("candidates", []) if _candidate_usable(candidate, str(case["city"]))]
        if not usable:
            failed_step = f"select_usable_candidate_{round_index}"
            failure_code = "NO_ACCEPTABLE_FROZEN_CANDIDATE"
            break
        interacted_candidates: set[str] = set()
        dismissed_candidates: set[str] = set()
        for command_index, interaction in enumerate(interaction_plan.get(round_index, []), start=1):
            candidate = _interaction_candidate(
                usable,
                interaction["target"],
                avoid=interacted_candidates,
                protected_place_ids=(
                    {expected_accept_place_id}
                    if interaction["event_type"] == "candidate_dismissed"
                    and expected_accept_place_id is not None
                    else None
                ),
            )
            if candidate is None:
                failed_step = f"{interaction['event_type']}_{round_index}_{command_index}"
                failure_code = "EVENT_COMMAND_CANDIDATE_UNAVAILABLE"
                break
            interacted_candidates.add(str(candidate.get("candidate_id")))
            if interaction["event_type"] == "candidate_dismissed":
                dismissed_candidates.add(str(candidate.get("candidate_id")))
            receipt, command_error = _run_interaction_command(
                recorder,
                case_id=case_id,
                workspace_id=workspace_id,
                suggestion_set=frozen,
                candidate=candidate,
                event_type=interaction["event_type"],
                reason_code=interaction.get("reason_code"),
                current_revision=current_revision,
                round_index=round_index,
                command_index=command_index,
                bearer_token=bearer_token,
            )
            if receipt is None:
                failed_step = f"{interaction['event_type']}_{round_index}_{command_index}"
                failure_code = command_error
                break
            event_commands.append(receipt)
        if failed_step is not None:
            break
        accepted_candidate = next((
            item
            for item in usable
            if str(item.get("candidate_id")) not in dismissed_candidates
            and (
                expected_accept_place_id is None
                or (item.get("canonical_place") or {}).get("place_id")
                == expected_accept_place_id
            )
        ), None)
        if accepted_candidate is None:
            failed_step = f"select_usable_candidate_{round_index}"
            failure_code = (
                "CAPTURED_CHAIN_ACCEPT_TARGET_NOT_USABLE"
                if expected_accept_place_id is not None
                else "ALL_USABLE_CANDIDATES_DISMISSED"
            )
            break
        candidate_id = accepted_candidate.get("candidate_id")
        idempotency_key = f"continuous-builder-accept-{suffix}-{round_index}"
        accept_response = recorder.request(
            case_id,
            f"accept_candidate_{round_index}",
            "POST",
            f"/api/trip-workspaces/{workspace_id}/suggestion-sets/{set_id}/candidates/{candidate_id}:accept",
            bearer_token=bearer_token,
            headers={"If-Match": f'"{current_revision}"', "Idempotency-Key": idempotency_key},
        )
        if accept_response.status_code != 200 or not isinstance(accept_response.body, dict):
            failed_step = f"accept_candidate_{round_index}"
            failure_code = _detail_code(accept_response) or f"HTTP_{accept_response.status_code}"
            rollback = recorder.request(
                case_id,
                f"rollback_readback_{round_index}",
                "GET",
                f"/api/trip-workspaces/{workspace_id}/snapshot",
                bearer_token=bearer_token,
            )
            if rollback.status_code == 200 and isinstance(rollback.body, dict):
                actual = rollback.body.get("current_revision", {}).get("revision")
                rounds.append(
                    {
                        "round_index": round_index,
                        "suggestion_set": frozen,
                        "create_hash": _frozen_hash(created),
                        "readback_hash": _frozen_hash(frozen),
                        "revision_before": current_revision,
                        "revision_after": actual,
                        "rollback_preserved_revision": actual == current_revision,
                    }
                )
            break
        accepted = accept_response.body
        new_revision = accepted.get("new_revision")
        rounds.append(
            {
                "round_index": round_index,
                "anchor_stop_id": anchor_stop_id,
                "suggestion_set": frozen,
                "create_hash": _frozen_hash(created),
                "readback_hash": _frozen_hash(frozen),
                "accepted_candidate_id": candidate_id,
                "accepted_canonical_place_id": (
                    accepted_candidate.get("canonical_place") or {}
                ).get("place_id"),
                "expected_captured_chain_place_id": expected_accept_place_id,
                "accepted_stop_id": accepted.get("stop_id"),
                "revision_before": current_revision,
                "revision_after": new_revision,
            }
        )
        if round_index == 1:
            replay = recorder.request(
                case_id,
                "replay_first_accept",
                "POST",
                f"/api/trip-workspaces/{workspace_id}/suggestion-sets/{set_id}/candidates/{candidate_id}:accept",
                bearer_token=bearer_token,
                headers={"If-Match": f'"{current_revision}"', "Idempotency-Key": idempotency_key},
            )
            idempotency_replayed = bool(
                replay.status_code == 200
                and isinstance(replay.body, dict)
                and replay.body.get("idempotent_replay") is True
            )
        if not isinstance(new_revision, int) or not isinstance(accepted.get("stop_id"), str):
            failed_step = f"accept_candidate_{round_index}"
            failure_code = "ACCEPT_REVISION_OR_STOP_MISSING"
            break
        current_revision = new_revision
        anchor_stop_id = accepted["stop_id"]

    if should_complete_line and failed_step is None:
        if last_frozen is None:
            failed_step = "line_completed"
            failure_code = "SUGGESTION_SET_UNAVAILABLE_FOR_LINE_COMPLETION"
        else:
            receipt, command_error = _run_interaction_command(
                recorder,
                case_id=case_id,
                workspace_id=workspace_id,
                suggestion_set=last_frozen,
                candidate=None,
                event_type="line_completed",
                reason_code=None,
                current_revision=current_revision,
                round_index=expected_rounds,
                command_index=1,
                bearer_token=bearer_token,
            )
            if receipt is None:
                failed_step = "line_completed"
                failure_code = command_error
            else:
                event_commands.append(receipt)

    events_response = recorder.request(
        case_id,
        "recommendation_events_readback",
        "GET",
        f"/api/trip-workspaces/{workspace_id}/recommendation-events",
        bearer_token=bearer_token,
    )
    events = events_response.body if events_response.status_code == 200 and isinstance(events_response.body, list) else []
    events = [event for event in events if isinstance(event, dict)]
    undo_attempted = "undo" in case.get("execution", {}).get("steps", []) and current_revision > 1
    undo_before = current_revision
    undo_after: int | None = None
    undo_event: dict[str, Any] | None = None
    undo_source_accept_event: dict[str, Any] | None = None
    if undo_attempted:
        before_undo_event_ids = {
            event.get("event_id") for event in events if isinstance(event.get("event_id"), str)
        }
        source_matches = [
            event
            for event in events
            if event.get("event_type") == "candidate_accepted" and event.get("revision_after") == undo_before
        ]
        if len(source_matches) == 1:
            undo_source_accept_event = source_matches[0]
        undo_response = recorder.request(
            case_id,
            "undo_last_accept",
            "POST",
            f"/api/trip-workspaces/{workspace_id}/undo",
            bearer_token=bearer_token,
            headers={
                "If-Match": f'"{current_revision}"',
                "Idempotency-Key": f"continuous-builder-undo-{suffix}",
            },
            json_body={
                "command_id": f"continuous-builder-undo-{suffix}",
                "base_revision": current_revision,
                "target_revision": current_revision - 1,
            },
        )
        if undo_response.status_code == 200 and isinstance(undo_response.body, dict):
            undo_after = undo_response.body.get("new_revision")
            if isinstance(undo_after, int):
                current_revision = undo_after
        elif failed_step is None:
            failed_step = "undo_last_accept"
            failure_code = _detail_code(undo_response) or f"HTTP_{undo_response.status_code}"
        post_undo_events = recorder.request(
            case_id,
            "recommendation_events_after_undo",
            "GET",
            f"/api/trip-workspaces/{workspace_id}/recommendation-events",
            bearer_token=bearer_token,
        )
        if post_undo_events.status_code == 200 and isinstance(post_undo_events.body, list):
            events = [event for event in post_undo_events.body if isinstance(event, dict)]
            undo_matches = [
                event
                for event in events
                if event.get("event_id") not in before_undo_event_ids
                and event.get("event_type") == "stop_undone"
                and event.get("revision_before") == undo_before
                and event.get("revision_after") == undo_after
            ]
            if len(undo_matches) == 1:
                undo_event = undo_matches[0]
    recovery_contracts = _run_recovery_contracts(
        recorder,
        case=case,
        bearer_token=bearer_token,
        provider_anchor=provider_anchor,
        run_namespace=run_namespace,
    )
    final_snapshot_response = recorder.request(
        case_id,
        "final_snapshot_readback",
        "GET",
        f"/api/trip-workspaces/{workspace_id}/snapshot",
        bearer_token=bearer_token,
    )
    final_snapshot = final_snapshot_response.body if final_snapshot_response.status_code == 200 else None
    resume_response = recorder.request(
        case_id,
        "fresh_client_resume_readback",
        "GET",
        f"/api/trip-workspaces/{workspace_id}/resume",
        bearer_token=bearer_token,
    )
    output = {
        "schema_version": "continuous-builder-product-output-v1",
        "case_id": case_id,
        "workspace_id": workspace_id,
        "seed_method": "public_create_workspace_initial_itinerary",
        "seed_provenance": (
            "frozen_snapshot_canonical_anchor_bootstrap_not_provider_receipt"
            if provider_anchor is not None
            else "controlled_evaluation_bootstrap_not_provider_evidence"
        ),
        "rounds": rounds,
        "events": events,
        "final_snapshot": final_snapshot,
        "fresh_client_resume_status_code": resume_response.status_code,
        "idempotency_replayed": idempotency_replayed,
        "event_commands": event_commands,
        "undo_attempted": undo_attempted,
        "undo_revision_before": undo_before if undo_attempted else None,
        "undo_revision_after": undo_after,
        "undo_event": undo_event,
        "undo_source_accept_event": undo_source_accept_event,
        "recovery_contracts": recovery_contracts,
        "unsupported_capabilities": _unsupported_capabilities(case),
        "failed_step": failed_step,
        "failure_code": failure_code,
    }
    return output, receipts, events


def _post_restart_case_readback(
    recorder: _Recorder,
    *,
    case: Mapping[str, Any],
    output: dict[str, Any],
    bearer_token: str,
    restart_gate: RestartGateResult,
) -> dict[str, Any]:
    """Read one already-completed Builder case through fresh public requests.

    The process restart itself is performed by the checked-in browser harness.
    This second readback binds that cohort-level service/Yjs proof to the exact
    workspace revision, content hash and recommendation ledger produced by this
    continuous run. No repository object or SQL connection crosses the boundary.
    """

    case_id = str(case["case_id"])
    workspace_id = str(output["workspace_id"])
    snapshot_response = recorder.request(
        case_id,
        "post_process_restart_fresh_snapshot_readback",
        "GET",
        f"/api/trip-workspaces/{workspace_id}/snapshot",
        bearer_token=bearer_token,
    )
    events_response = recorder.request(
        case_id,
        "post_process_restart_fresh_event_ledger_readback",
        "GET",
        f"/api/trip-workspaces/{workspace_id}/recommendation-events",
        bearer_token=bearer_token,
    )
    snapshot = snapshot_response.body if snapshot_response.status_code == 200 else None
    events = events_response.body if events_response.status_code == 200 else None
    expected_snapshot = output.get("final_snapshot")
    expected_events = output.get("events")
    expected_revision = (
        expected_snapshot.get("current_revision")
        if isinstance(expected_snapshot, dict)
        else None
    )
    actual_revision = snapshot.get("current_revision") if isinstance(snapshot, dict) else None
    exact_revision = bool(
        isinstance(expected_revision, dict)
        and isinstance(actual_revision, dict)
        and actual_revision.get("revision") == expected_revision.get("revision")
        and actual_revision.get("content_hash") == expected_revision.get("content_hash")
    )
    exact_events = isinstance(events, list) and events == expected_events
    passed = restart_gate.passed and exact_revision and exact_events
    return {
        "status": "PASS" if passed else "FAIL",
        "transport": "PUBLIC_HTTP_AND_SEPARATE_BROWSER_YJS_GATE",
        "claim_scope": "local_fixture_process_restart_recovery",
        "restart_gate_reason_code": restart_gate.reason_code,
        "backend_snapshot_status_code": snapshot_response.status_code,
        "event_ledger_status_code": events_response.status_code,
        "revision_exact": exact_revision,
        "events_exact": exact_events,
        "expected_revision": expected_revision.get("revision") if isinstance(expected_revision, dict) else None,
        "actual_revision": actual_revision.get("revision") if isinstance(actual_revision, dict) else None,
        "expected_content_hash": expected_revision.get("content_hash") if isinstance(expected_revision, dict) else None,
        "actual_content_hash": actual_revision.get("content_hash") if isinstance(actual_revision, dict) else None,
    }


def run_builder_http(
    spec_path: str | Path,
    *,
    runs_root: str | Path | None = None,
    repo_root: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
    transport: HttpTransport | None = None,
    bearer_token: str | None = None,
    timeout_seconds: float = 3.0,
    restart_gate_runner: Callable[..., RestartGateResult] | None = None,
) -> RunResult:
    started_at = time.time()
    started_at_iso = datetime.now(timezone.utc).isoformat()
    preflight_result = preflight(spec_path, repo_root=repo_root, environ=environ)
    lane = preflight_result.resolved_spec.get("lane", "invalid") if preflight_result.resolved_spec else "invalid"
    output_root = Path(runs_root).resolve() if runs_root else preflight_result.repo_root / "backend" / "evidence" / "runs"
    run_id, run_dir = _new_run_dir(output_root, str(lane))
    artifact_spec = copy.deepcopy(preflight_result.resolved_spec) if preflight_result.resolved_spec else {
        "schema_version": "invalid-run-spec-v1",
        "source_path": str(preflight_result.spec_path),
    }
    artifact_spec["run_id"] = run_id
    artifact_spec["started_at"] = started_at_iso
    _atomic_write_json(run_dir / "run_spec.json", artifact_spec)
    product_outputs: list[dict[str, Any]] = []
    provider_receipts: list[dict[str, Any]] = []
    recommendation_events: list[dict[str, Any]] = []
    scores: list[dict[str, Any]] = []
    bad_cases: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    recorder: _Recorder | None = None
    provider_handshake: dict[str, Any] | None = None
    restart_gate_result = RestartGateResult(
        "UNAVAILABLE",
        "RESTART_GATE_NOT_REQUIRED_OR_NOT_RUN",
        {
            "schema_version": "backend-yjs-restart-gate-v1",
            "status": "UNAVAILABLE",
        },
    )
    if not preflight_result.valid:
        reason = "PREFLIGHT_FAILED"
        errors.extend(preflight_result.errors)
    else:
        try:
            cases, labels = _load_selected_builder_cases_and_labels(artifact_spec, preflight_result.repo_root)
            sut = artifact_spec.get("sut", {})
            if sut.get("allow_direct_domain_calls") is not False or sut.get("allow_sql_seed") is not False:
                raise ValueError("HTTP_ONLY_AND_NO_SQL_SEED_CONTRACT_REQUIRED")
            recorder = _Recorder(transport or UrllibTransport(), str(sut["base_url"]), timeout_seconds)
            provider_handshake = _verify_product_provider_handshake(
                recorder,
                artifact_spec.get("provider", {}),
            )
            provider_anchors = _frozen_snapshot_anchors(artifact_spec, preflight_result.repo_root)
            token = bearer_token
            if not token:
                login = recorder.request("__run__", "auth_test_login", "POST", "/api/auth/test-login")
                login_body = _expect(login, {200}, "auth_test_login")
                token = login_body.get("token")
                if not isinstance(token, str) or not token:
                    raise RuntimeError("auth_test_login returned no bearer token")
            completed: list[
                tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]
            ] = []
            for case in cases:
                try:
                    output, receipts, events = _execute_case(
                        recorder,
                        case,
                        token,
                        provider_anchor=provider_anchors.get(str(case["city"])),
                        run_namespace=run_id,
                    )
                    product_outputs.append(output)
                    provider_receipts.extend(receipts)
                    recommendation_events.extend(
                        {"case_id": case["case_id"], **event} for event in events
                    )
                    completed.append((case, output, receipts, events))
                except Exception as exc:
                    bad_cases.append({"case_id": case["case_id"], "reason": "HTTP_WORKFLOW_FAILED", "message": str(exc)})

            restart_cases = [item for item in completed if "restart_backend_yjs" in _required_steps(item[0])]
            if restart_cases:
                recovery_config = artifact_spec.get("recovery")
                recovery_config = recovery_config if isinstance(recovery_config, dict) else {}
                backend_yjs = recovery_config.get("backend_yjs_restart")
                backend_yjs = backend_yjs if isinstance(backend_yjs, dict) else {}
                command_id = str(backend_yjs.get("command_id") or "")
                runner = restart_gate_runner or run_checked_in_restart_gate
                restart_gate_result = runner(
                    preflight_result.repo_root,
                    environ=dict(os.environ if environ is None else environ),
                    command_id=command_id,
                )
                for case, output, _receipts, _events in restart_cases:
                    contract = _post_restart_case_readback(
                        recorder,
                        case=case,
                        output=output,
                        bearer_token=token,
                        restart_gate=restart_gate_result,
                    ) if restart_gate_result.passed else {
                        "status": "FAIL",
                        "transport": "PUBLIC_HTTP_AND_SEPARATE_BROWSER_YJS_GATE",
                        "claim_scope": "local_fixture_process_restart_recovery",
                        "restart_gate_reason_code": restart_gate_result.reason_code,
                    }
                    output.setdefault("recovery_contracts", {})["backend_yjs_restart"] = contract
                    output["unsupported_capabilities"] = _unsupported_capabilities(
                        case,
                        restart_gate_passed=contract.get("status") == "PASS",
                    )

            for case, output, receipts, events in completed:
                score = _score_case(case, labels[case["case_id"]], output, receipts, events)
                scores.append(score)
                if score["status"] != "PASS":
                    bad_cases.append(
                        {
                            "case_id": case["case_id"],
                            "reason": "DETERMINISTIC_SCORE_OR_CAPABILITY_FAILED",
                            "failed_step": output.get("failed_step"),
                            "failure_code": output.get("failure_code"),
                            "unsupported_capabilities": output.get("unsupported_capabilities", []),
                        }
                    )
            reason = "BUILDER_HTTP_SLICE_COMPLETE" if not bad_cases else "BUILDER_HTTP_CASES_FAILED_OR_UNSUPPORTED"
        except Exception as exc:
            reason = "PRODUCT_HTTP_ADAPTER_UNAVAILABLE"
            errors.append({"code": reason, "message": str(exc)})
    transactions = recorder.transactions if recorder else []
    accept_place_body_count = sum(
        1
        for item in transactions
        if str(item.get("step", "")).startswith(("accept_candidate_", "replay_first_accept"))
        and item.get("request_body") is not None
    )
    metric_scores = [score["metric_score"] for score in scores]
    metric_aggregate = aggregate_metric_scores(metric_scores)
    metric_gates = evaluate_metric_thresholds(metric_aggregate, artifact_spec.get("thresholds", {}))
    all_pass = (
        preflight_result.valid
        and reason == "BUILDER_HTTP_SLICE_COMPLETE"
        and accept_place_body_count == 0
        and all(item["status"] == "PASS" for item in metric_gates)
    )
    _atomic_write_jsonl(run_dir / "product_outputs.jsonl", product_outputs)
    _atomic_write_jsonl(run_dir / "provider_receipts.jsonl", provider_receipts)
    _atomic_write_jsonl(run_dir / "recommendation_events.jsonl", recommendation_events)
    _atomic_write_jsonl(run_dir / "http_transactions.jsonl", transactions)
    _atomic_write_json(
        run_dir / "deterministic_scores.json",
        {"cases": scores, "metric_aggregate": metric_aggregate, "metric_gates": metric_gates},
    )
    _atomic_write_jsonl(run_dir / "bad_cases.jsonl", bad_cases)
    _atomic_write_json(run_dir / "restart_gate.json", restart_gate_result.receipt)
    _atomic_write_json(
        run_dir / "cost.json",
        {
            "schema_version": "continuous-cost-v1",
            "currency": "CNY",
            "paid_api_calls": 0,
            "total_cost": 0,
            "declared_budget": artifact_spec.get("budget", {}).get("max_total_cost_cny"),
        },
    )
    gate = {
        "schema_version": "continuous-gate-v1",
        "run_id": run_id,
        "lane": lane,
        "status": "PASS" if all_pass else "INVALID",
        "decision": "PROMOTE" if all_pass else "REJECT",
        "phase": "BUILDER_SUGGESTION_HTTP",
        "started_at_epoch": started_at,
        "completed_at_epoch": time.time(),
        "run_spec_artifact_sha256": _sha256_bytes((run_dir / "run_spec.json").read_bytes()),
        "bindings": preflight_result.bindings,
        "gates": [
            *preflight_result.checks,
            *metric_gates,
            {"id": "PRODUCT_HTTP_EXECUTION", "status": "PASS" if all_pass else "FAIL", "reason": reason},
        ],
        "failed_cases": [item["case_id"] for item in bad_cases],
        "execution": {
            "attempted": bool(transactions),
            "product_http_calls": len(transactions),
            "adapter": "breezetravel-builder-suggestion-http-v1",
            "direct_domain_calls": 0,
            "sql_seed_operations": 0,
            "client_place_bodies_on_accept": accept_place_body_count,
            "selected_case_count": preflight_result.bindings.get("selected_case_count", 0),
            "completed_case_count": len(product_outputs),
            "reason": reason,
            "provider_handshake": provider_handshake,
            "restart_gate": {
                "status": restart_gate_result.status,
                "reason_code": restart_gate_result.reason_code,
            },
        },
        "errors": errors,
    }
    _atomic_write_json(run_dir / "gate.json", gate)
    return RunResult(run_id=run_id, run_dir=run_dir, gate=gate)
