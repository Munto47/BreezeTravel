from __future__ import annotations

import copy
import hashlib
import json
import threading
import urllib.parse
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest

from evals.continuous import HttpResponse, run_builder_http
from evals.continuous import http_builder as builder_module
from evals.continuous.core import preflight
from evals.continuous.restart_gate import RestartGateResult


BACKEND_ROOT = Path(__file__).resolve().parents[1]
SPEC = BACKEND_ROOT / "evals" / "run_specs" / "dual-entry-builder-http-slice.json"
G2_CHAIN_SPEC = BACKEND_ROOT / "evals" / "run_specs" / "dual-entry-g2-four-stop-snapshot.json"
DATASET = BACKEND_ROOT / "eval_data" / "dual_entry_v1"


def _rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _selected() -> tuple[list[dict], dict[str, dict]]:
    spec = json.loads(SPEC.read_text(encoding="utf-8"))
    selected = spec["dataset"]["case_ids"]
    cases = {
        row["case_id"]: row
        for path in DATASET.glob("*.inputs.jsonl")
        for row in _rows(path)
        if row["case_id"] in selected
    }
    labels = {
        row["case_id"]: row
        for path in DATASET.glob("*.labels.jsonl")
        for row in _rows(path)
        if row["case_id"] in selected
    }
    return [cases[case_id] for case_id in selected], labels


def _valid_preflight():
    result = preflight(SPEC, environ={})
    return replace(
        result,
        checks=tuple({**check, "status": "PASS"} for check in result.checks),
        errors=(),
    )


def test_g2_chain_run_spec_bootstraps_exact_three_city_selected_chains():
    spec = json.loads(G2_CHAIN_SPEC.read_text(encoding="utf-8"))
    anchors = builder_module._frozen_snapshot_anchors(spec, BACKEND_ROOT.parent)

    assert set(anchors) == {"北京", "上海", "杭州"}
    for city, anchor in anchors.items():
        chain = anchor["selected_chain_place_ids"]
        assert len(chain) == len(set(chain)) == 4
        assert chain[0] == anchor["place_id"]
        assert [
            builder_module._captured_chain_accept_place_id(anchor, round_index) for round_index in (1, 2, 3)
        ] == chain[1:]


def test_dismiss_fallback_cannot_consume_captured_chain_accept_target():
    candidates = [
        {
            "candidate_id": "ephemeral-first",
            "canonical_place": {"place_id": "captured-next"},
            "provider_receipt": {"provider_place_id": "captured-next"},
        },
        {
            "candidate_id": "ephemeral-second",
            "canonical_place": {"place_id": "safe-dismiss"},
            "provider_receipt": {"provider_place_id": "safe-dismiss"},
        },
    ]

    selected = builder_module._interaction_candidate(
        candidates,
        "synthetic-target-not-in-live-snapshot",
        avoid=set(),
        protected_place_ids={"captured-next"},
    )

    assert selected is candidates[1]


def _supported_three_city_cases() -> tuple[list[dict], dict[str, dict]]:
    cases, labels = _selected()
    selected = [cases[0], cases[2], cases[4]]
    supported = []
    for case in selected:
        item = copy.deepcopy(case)
        item["execution"]["steps"] = [
            "create_workspace",
            "search_seed",
            "accept_seed",
            "request_suggestions",
            "accept_candidate",
            "readback",
        ]
        original_steps = case.get("execution", {}).get("steps", [])
        for interaction_step in ("preview_candidate", "dismiss_candidate"):
            if interaction_step in original_steps:
                item["execution"]["steps"].append(interaction_step)
        item["input"]["action_sequence"] = [
            action
            for action in item["input"].get("action_sequence", [])
            if not action.startswith(("undo:", "restart_", "new_client"))
        ]
        supported.append(item)
    return supported, {case["case_id"]: labels[case["case_id"]] for case in supported}


class BuilderFixtureTransport:
    def __init__(
        self,
        *,
        leak: str | None = None,
        accept_error: str | None = None,
        provider_unavailable: bool = False,
        event_failure: str | None = None,
        provider_identity_mismatch: bool = False,
    ):
        self.leak = leak
        self.accept_error = accept_error
        self.provider_unavailable = provider_unavailable
        self.event_failure = event_failure
        self.provider_identity_mismatch = provider_identity_mismatch
        self.workspaces: dict[str, dict] = {}
        self.sets: dict[str, dict] = {}
        self.events: dict[str, list[dict]] = {}
        self.idempotency: dict[str, dict] = {}
        self.edit_lock = threading.Lock()

    @staticmethod
    def _content_hash(days: list[dict]) -> str:
        semantic = [
            {
                "day_index": day["day_index"],
                "stops": [
                    {
                        "stop_id": stop["stop_id"],
                        "place_id": stop["place_id"],
                        "day_index": day["day_index"],
                        "order_index": index,
                        "locked": bool(stop.get("locked")),
                    }
                    for index, stop in enumerate(day["stops"])
                ],
            }
            for day in days
        ]
        raw = json.dumps(semantic, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode()).hexdigest()

    @classmethod
    def _revision(cls, state: dict) -> dict:
        days = copy.deepcopy(state["days"])
        for day in days:
            for index, stop in enumerate(day["stops"]):
                stop["day_index"] = day["day_index"]
                stop["order_index"] = index
        return {
            "workspace_id": state["workspace_id"],
            "revision": state["revision"],
            "content_hash": cls._content_hash(days),
            "days": days,
        }

    @staticmethod
    def _event(workspace_id: str, event_type: str, index: int, **extra) -> dict:
        return {
            "event_id": f"event-{workspace_id}-{index}",
            "session_id": extra.pop("session_id", "session"),
            "workspace_id": workspace_id,
            "actor_id": "eval-user",
            "event_type": event_type,
            "revision_before": extra.pop("revision_before", None),
            "revision_after": extra.pop("revision_after", None),
            "suggestion_set_id": extra.pop("suggestion_set_id", None),
            "candidate_id": extra.pop("candidate_id", None),
            "context_hash": extra.pop("context_hash", "c" * 64),
            "policy_version": extra.pop("policy_version", "builder-policy-test-v1"),
            "provider_snapshot_id": extra.pop("provider_snapshot_id", "snapshot-test-v1"),
            "rank_position": extra.pop("rank_position", None),
            "reason_code": extra.pop("reason_code", None),
            "payload": extra,
            "occurred_at": "2026-08-21T00:00:00+00:00",
        }

    @staticmethod
    def _receipt(workspace_id: str, round_index: int, rank: int, city: str) -> dict:
        place_id = f"{workspace_id}-place-{round_index}-{rank}"
        return {
            "canonical_place_id": place_id,
            "provider": "amap",
            "provider_place_id": place_id,
            "name": f"候选{round_index}-{rank}",
            "city": city,
            "district": "测试区",
            "address": "测试地址",
            "category": "attraction" if rank != 3 else "food",
            "longitude": 116.0 + rank / 100,
            "latitude": 39.0 + rank / 100,
            "request_hash": "a" * 64,
            "response_hash": f"{round_index}{rank}".ljust(64, "b")[:64],
            "observed_at": "2026-08-21T00:00:00+00:00",
            "execution_mode": "live",
            "source_url": "https://restapi.amap.com/",
        }

    def _suggestion_set(self, workspace_id: str, body: dict) -> dict:
        state = self.workspaces[workspace_id]
        round_index = state["set_round"] + 1
        state["set_round"] = round_index
        candidates = []
        for rank in range(1, 5):
            receipt = self._receipt(workspace_id, round_index, rank, state["city"])
            city = state["city"]
            passed = True
            freshness = "FRESH"
            route_status = "AVAILABLE"
            if rank == 1 and self.leak == "wrong_city":
                city = "上海" if city != "上海" else "杭州"
                receipt["city"] = city
            if rank == 1 and self.leak == "hard":
                passed = False
            if rank == 1 and self.leak == "unknown":
                freshness = "UNKNOWN"
                route_status = "UNKNOWN"
            candidates.append(
                {
                    "candidate_id": f"candidate-{round_index}-{rank}",
                    "suggestion_set_id": f"set-{workspace_id}-{round_index}",
                    "canonical_place": {
                        "place_id": receipt["canonical_place_id"],
                        "name": receipt["name"],
                        "city": city,
                        "district": "测试区",
                        "address": "测试地址",
                        "category": receipt["category"],
                        "coords": {"lng": receipt["longitude"], "lat": receipt["latitude"]},
                    },
                    "provider_receipt": receipt,
                    "provider_receipt_id": f"receipt-{round_index}-{rank}",
                    "rank_position": rank,
                    "classification": "ON_ROUTE" if passed else "INFEASIBLE",
                    "source_prior_refs": ["official-test-prior", "open-user-test-prior"],
                    "score_components": {"route": 1.0, "prior": 0.5},
                    "total_score": 1.5 - rank / 10,
                    "hard_gate": {"passed": passed, "reason_codes": [] if passed else ["HARD_CONSTRAINT"]},
                    "route_delta": {
                        "status": route_status,
                        "delta_route_minutes": rank * 3 if route_status == "AVAILABLE" else None,
                        "previous_to_candidate_minutes": rank * 2 if route_status == "AVAILABLE" else None,
                        "candidate_to_next_minutes": None,
                        "previous_to_next_minutes": None,
                        "reason_code": None if route_status == "AVAILABLE" else "ROUTE_UNKNOWN",
                    },
                    "evidence_freshness": {
                        "status": freshness,
                        "observed_at": "2026-08-21T00:00:00+00:00" if freshness == "FRESH" else None,
                        "max_age_seconds": 86400,
                        "reason_code": None if freshness == "FRESH" else "EVIDENCE_UNKNOWN",
                    },
                    "explanation_codes": ["NEARBY", "OFFICIAL_AND_OPEN_USER_PRIORS"],
                }
            )
        set_id = f"set-{workspace_id}-{round_index}"
        result = {
            "suggestion_set_id": set_id,
            "workspace_id": workspace_id,
            "base_revision": body["base_revision"],
            "day_index": body["day_index"],
            "insert_after_stop_id": body["insert_after_stop_id"],
            "insert_before_stop_id": body["insert_before_stop_id"],
            "intents": body["intents"],
            "context_hash": "c" * 64,
            "policy_version": "builder-policy-test-v1",
            "provider_snapshot_id": "snapshot-test-v1",
            "expires_at": "2027-08-21T00:00:00+00:00",
            "session_id": body["session_id"],
            "candidates": candidates,
            "created_by": "eval-user",
            "created_at": "2026-08-21T00:00:00+00:00",
        }
        self.sets[set_id] = result
        events = self.events[workspace_id]
        events.append(
            self._event(
                workspace_id,
                "suggestions_shown",
                len(events),
                session_id=body["session_id"],
                revision_before=body["base_revision"],
                suggestion_set_id=set_id,
            )
        )
        return result

    def request(self, method, url, *, headers, json_body, timeout_seconds):
        del timeout_seconds
        path = urllib.parse.urlparse(url).path
        if path == "/health":
            spec = json.loads(SPEC.read_text(encoding="utf-8"))
            provider = spec["provider"]
            return HttpResponse(
                200,
                {},
                {
                    "status": "ok",
                    "suggestion_provider": {
                        "mode": provider["mode"],
                        "snapshot_id": (
                            "wrong-snapshot-id" if self.provider_identity_mismatch else provider["snapshot_id"]
                        ),
                        "snapshot_sha256": provider["snapshot_sha256"],
                        "replay_at": "2026-08-21T04:11:29.399183+00:00",
                    },
                },
            )
        if path == "/api/auth/test-login":
            return HttpResponse(200, {}, {"token": "secret-builder-token", "user_id": "eval-user"})
        if path == "/api/room":
            return HttpResponse(200, {}, {"status": "ok", "room_id": json_body["room_id"]})
        if path == "/api/trip-workspaces" and method == "POST":
            workspace_id = json_body["workspace_id"]
            itinerary = json_body["initial_itinerary"]
            seed_slot = next(slot for day in itinerary["days"] for slot in day["slots"])
            days = []
            for day in itinerary["days"]:
                stops = []
                for index, slot in enumerate(day["slots"]):
                    stops.append(
                        {
                            "stop_id": (
                                f"seed-stop-{itinerary['itinerary_id']}-{day['day_index']}-{index}-{slot['place_id']}"
                            ),
                            "place_id": slot["place_id"],
                            "locked": False,
                        }
                    )
                days.append({"day_index": day["day_index"], "stops": stops})
            self.workspaces[workspace_id] = {
                "workspace_id": workspace_id,
                "city": json_body["city"],
                "revision": 1,
                "seed_place_id": seed_slot["place_id"],
                "days": days,
                "set_round": 0,
            }
            self.workspaces[workspace_id]["revisions"] = {1: self._revision(self.workspaces[workspace_id])}
            self.events[workspace_id] = []
            return HttpResponse(201, {}, {"workspace_id": workspace_id, "current_itinerary_revision": 1})
        if path.endswith("/snapshot"):
            workspace_id = path.split("/")[-2]
            state = self.workspaces[workspace_id]
            return HttpResponse(
                200,
                {"ETag": f'"{state["revision"]}"'},
                {
                    "workspace": {"workspace_id": workspace_id, "current_itinerary_revision": state["revision"]},
                    "current_revision": copy.deepcopy(state["revisions"][state["revision"]]),
                },
            )
        if "/revisions/" in path and method == "GET":
            parts = path.split("/")
            workspace_id = parts[3]
            revision = int(parts[-1])
            return HttpResponse(200, {}, copy.deepcopy(self.workspaces[workspace_id]["revisions"][revision]))
        if path.endswith("/edits") and method == "POST":
            workspace_id = path.split("/")[3]
            with self.edit_lock:
                state = self.workspaces[workspace_id]
                base_revision = json_body["base_revision"]
                if state["revision"] != base_revision:
                    return HttpResponse(
                        409,
                        {},
                        {
                            "detail": {
                                "code": "ITINERARY_REVISION_CONFLICT",
                                "expected_revision": base_revision,
                                "actual_revision": state["revision"],
                            }
                        },
                    )
                before_days = copy.deepcopy(state["days"])
                stop_id = json_body["payload"]["stop_id"]
                located = next(
                    (item for day in state["days"] for item in day["stops"] if item["stop_id"] == stop_id),
                    None,
                )
                assert located is not None
                operation = json_body["operation"]
                changed_days = []
                if operation == "MOVE_TO_DAY":
                    source_day = next(day for day in state["days"] if located in day["stops"])
                    target_day = state["days"][json_body["payload"]["target_day_index"]]
                    source_day["stops"].remove(located)
                    target_day["stops"].insert(json_body["payload"]["target_order_index"], located)
                    changed_days = sorted({source_day["day_index"], target_day["day_index"]})
                elif operation == "LOCK_STOP":
                    located["locked"] = True
                    changed_days = [day["day_index"] for day in state["days"] if located in day["stops"]]
                else:
                    raise AssertionError(f"unsupported fixture edit operation: {operation}")
                state["revision"] += 1
                revision = self._revision(state)
                state["revisions"][state["revision"]] = revision
                changed_edges = [] if before_days == state["days"] else ["fixture-edge-change"]
                result = {
                    "accepted": True,
                    "command_id": json_body["command_id"],
                    "new_revision": state["revision"],
                    "changed_days": changed_days,
                    "changed_route_edges": changed_edges,
                    "route_delta": {"status": "UNAVAILABLE", "changed_days": changed_days},
                    "incremental_findings": [],
                    "affected_rule_ids": ["constraint.time_chain", "route.gap"],
                    "audit_mode": "INCREMENTAL_REVISION_ONLY",
                    "llm_calls": 0,
                    "report_stale": True,
                    "idempotent_replay": False,
                }
                return HttpResponse(200, {"ETag": f'"{state["revision"]}"'}, result)
        if path.endswith("/audits") and method == "POST":
            workspace_id = path.split("/")[3]
            state = self.workspaces[workspace_id]
            return HttpResponse(
                200,
                {},
                {
                    "report_id": f"report-{workspace_id}-{state['revision']}",
                    "workspace_id": workspace_id,
                    "itinerary_revision": state["revision"],
                    "findings": [],
                },
            )
        if path.endswith("/suggestion-sets") and method == "POST":
            if self.provider_unavailable:
                return HttpResponse(503, {}, {"detail": {"code": "SUGGESTION_PROVIDER_UNAVAILABLE"}})
            workspace_id = path.split("/")[3]
            return HttpResponse(201, {}, self._suggestion_set(workspace_id, json_body))
        if "/suggestion-sets/" in path and method == "GET":
            set_id = path.split("/")[-1]
            return HttpResponse(200, {}, copy.deepcopy(self.sets[set_id]))
        if (
            path.endswith(":preview") or path.endswith(":dismiss") or path.endswith(":line-completed")
        ) and method == "POST":
            if self.event_failure == "http":
                return HttpResponse(503, {}, {"detail": {"code": "EVENT_COMMAND_UNAVAILABLE"}})
            workspace_id = path.split("/")[3]
            is_line_completed = path.endswith(":line-completed")
            if is_line_completed:
                set_id = path.split("/")[5].removesuffix(":line-completed")
                candidate_id = None
                event_type = "line_completed"
                reason_code = None
            else:
                set_id = path.split("/")[5]
                suffix = path.split("/")[7]
                candidate_id = suffix.split(":", 1)[0]
                event_type = "candidate_previewed" if path.endswith(":preview") else "candidate_dismissed"
                reason_code = json_body["reason_code"] if event_type == "candidate_dismissed" else None
            key = headers["Idempotency-Key"]
            if key in self.idempotency:
                replay = copy.deepcopy(self.idempotency[key])
                replay["idempotent_replay"] = self.event_failure != "replay"
                return HttpResponse(200, {"Idempotency-Replayed": "true"}, replay)
            suggestion_set = self.sets[set_id]
            rank_position = None
            if candidate_id is not None:
                candidate = next(item for item in suggestion_set["candidates"] if item["candidate_id"] == candidate_id)
                rank_position = candidate["rank_position"]
            event = self._event(
                workspace_id,
                event_type,
                len(self.events[workspace_id]),
                session_id=suggestion_set["session_id"],
                revision_before=self.workspaces[workspace_id]["revision"],
                suggestion_set_id=set_id,
                candidate_id=candidate_id,
                rank_position=rank_position,
                context_hash=suggestion_set["context_hash"],
                policy_version=suggestion_set["policy_version"],
                provider_snapshot_id=suggestion_set["provider_snapshot_id"],
                reason_code=reason_code,
            )
            if self.event_failure == "context":
                event["context_hash"] = "f" * 64
            self.events[workspace_id].append(event)
            result = {"event": event, "idempotent_replay": False}
            self.idempotency[key] = copy.deepcopy(result)
            return HttpResponse(200, {}, result)
        if path.endswith(":accept") and method == "POST":
            workspace_id = path.split("/")[3]
            set_id = path.split("/")[5]
            candidate_id = path.split("/")[7].removesuffix(":accept")
            key = headers["Idempotency-Key"]
            if key in self.idempotency:
                replay = copy.deepcopy(self.idempotency[key])
                replay["idempotent_replay"] = True
                return HttpResponse(200, {"Idempotency-Replayed": "true"}, replay)
            if self.accept_error:
                return HttpResponse(409, {}, {"detail": {"code": self.accept_error}})
            state = self.workspaces[workspace_id]
            suggestion_set = self.sets[set_id]
            candidate = next(item for item in suggestion_set["candidates"] if item["candidate_id"] == candidate_id)
            before = state["revision"]
            state["revision"] += 1
            stop_id = f"accepted-stop-{workspace_id}-{state['revision']}"
            state["days"][0]["stops"].append(
                {"stop_id": stop_id, "place_id": candidate["canonical_place"]["place_id"], "locked": False}
            )
            state["revisions"][state["revision"]] = self._revision(state)
            event = self._event(
                workspace_id,
                "candidate_accepted",
                len(self.events[workspace_id]),
                session_id=suggestion_set["session_id"],
                revision_before=before,
                revision_after=state["revision"],
                suggestion_set_id=set_id,
                candidate_id=candidate_id,
                rank_position=candidate["rank_position"],
                stop_id=stop_id,
                canonical_place_id=candidate["canonical_place"]["place_id"],
            )
            self.events[workspace_id].append(event)
            response = {
                "accepted": True,
                "suggestion_set_id": set_id,
                "candidate_id": candidate_id,
                "new_revision": state["revision"],
                "stop_id": stop_id,
                "revision": {"revision": state["revision"]},
                "event": event,
                "idempotent_replay": False,
            }
            self.idempotency[key] = copy.deepcopy(response)
            return HttpResponse(200, {"ETag": f'"{state["revision"]}"'}, response)
        if path.endswith("/recommendation-events"):
            workspace_id = path.split("/")[3]
            events = copy.deepcopy(self.events[workspace_id])
            if self.event_failure == "readback":
                events = [event for event in events if event["event_type"] != "candidate_previewed"]
            return HttpResponse(200, {}, events)
        if path.endswith("/undo") and method == "POST":
            workspace_id = path.split("/")[3]
            state = self.workspaces[workspace_id]
            before = state["revision"]
            accepted = next(
                event
                for event in reversed(self.events[workspace_id])
                if event["event_type"] == "candidate_accepted" and event["revision_after"] == before
            )
            state["revision"] += 1
            state["days"][0]["stops"] = state["days"][0]["stops"][:-1]
            state["revisions"][state["revision"]] = self._revision(state)
            self.events[workspace_id].append(
                self._event(
                    workspace_id,
                    "stop_undone",
                    len(self.events[workspace_id]),
                    session_id=accepted["session_id"],
                    revision_before=before,
                    revision_after=state["revision"],
                    suggestion_set_id=accepted["suggestion_set_id"],
                    candidate_id=accepted["candidate_id"],
                    rank_position=accepted["rank_position"],
                    context_hash=accepted["context_hash"],
                    policy_version=accepted["policy_version"],
                    provider_snapshot_id=accepted["provider_snapshot_id"],
                    reason_code="UNDO_ACCEPTED_SUGGESTION",
                    source_accept_event_id=accepted["event_id"],
                    source_accept_revision=before,
                    target_revision=json_body["target_revision"],
                    stop_id=accepted["payload"]["stop_id"],
                    canonical_place_id=accepted["payload"]["canonical_place_id"],
                )
            )
            return HttpResponse(200, {}, {"new_revision": state["revision"]})
        if path.endswith("/resume"):
            workspace_id = path.split("/")[-2]
            return HttpResponse(200, {"ETag": '"resume"'}, {"workspace_id": workspace_id})
        raise AssertionError(f"unexpected request: {method} {path}")


def _run(tmp_path, monkeypatch, transport, *, cases=None, restart_gate_runner=None):
    valid = _valid_preflight()
    if cases is not None:
        _, labels = cases
        has_ranking_oracle = any(
            label["metric_oracles"][metric]["applicability"] == "APPLICABLE"
            for label in labels.values()
            for metric in ("builder_ndcg_at_5", "builder_recall_at_5")
        )
        if not has_ranking_oracle:
            resolved = copy.deepcopy(valid.resolved_spec)
            resolved["thresholds"].pop("builder_ndcg_at_5", None)
            resolved["thresholds"].pop("builder_recall_at_5", None)
            valid = replace(valid, resolved_spec=resolved)
    monkeypatch.setattr(builder_module, "preflight", lambda *args, **kwargs: valid)
    if cases is not None:
        monkeypatch.setattr(builder_module, "_load_selected_builder_cases_and_labels", lambda *args: cases)
    return run_builder_http(
        SPEC,
        runs_root=tmp_path / "runs",
        transport=transport,
        environ={},
        restart_gate_runner=restart_gate_runner,
    )


def test_three_city_four_stop_slice_rejects_below_nine_sessions_and_never_sends_place_body_on_accept(
    tmp_path, monkeypatch
):
    cases = _supported_three_city_cases()
    result = _run(tmp_path, monkeypatch, BuilderFixtureTransport(), cases=cases)

    assert result.gate["status"] == "INVALID"
    assert result.gate["decision"] == "REJECT"
    assert result.gate["execution"]["completed_case_count"] == 3
    session_gate = next(item for item in result.gate["gates"] if item["id"] == "G2_FOUR_STOP_SESSION_COUNT")
    assert session_gate["status"] == "FAIL"
    assert session_gate["actual"] == 3
    assert session_gate["threshold"] == 9
    assert result.gate["execution"]["direct_domain_calls"] == 0
    assert result.gate["execution"]["sql_seed_operations"] == 0
    assert result.gate["execution"]["client_place_bodies_on_accept"] == 0
    persisted_spec = json.loads((result.run_dir / "run_spec.json").read_text(encoding="utf-8"))
    assert persisted_spec["run_id"] == result.run_id
    persisted_started_at = datetime.fromisoformat(persisted_spec["started_at"])
    assert persisted_started_at.tzinfo is not None
    assert persisted_started_at.utcoffset() is not None
    assert "run_id" not in json.loads(SPEC.read_text(encoding="utf-8"))
    outputs = _rows(result.run_dir / "product_outputs.jsonl")
    assert {item["case_id"].split(".")[1] for item in outputs} == {"bj", "sh", "hz"}
    assert all(len(item["rounds"]) == 3 for item in outputs)
    transactions = _rows(result.run_dir / "http_transactions.jsonl")
    accepts = [row for row in transactions if row["step"].startswith("accept_candidate_")]
    assert accepts and all(row["request_body"] is None for row in accepts)
    serialized = json.dumps(transactions, ensure_ascii=False)
    assert "secret-builder-token" not in serialized
    assert "<redacted>" in serialized
    assert _rows(result.run_dir / "provider_receipts.jsonl")
    assert _rows(result.run_dir / "recommendation_events.jsonl")
    event_commands = [command for item in outputs for command in item["event_commands"]]
    assert {command["event_type"] for command in event_commands} == {
        "candidate_previewed",
        "candidate_dismissed",
        "line_completed",
    }
    assert all(command["frozen_context_valid"] for command in event_commands)
    assert all(command["idempotent_replay"] for command in event_commands)
    for output in outputs:
        for command in output["event_commands"]:
            frozen = next(
                item["suggestion_set"]
                for item in output["rounds"]
                if item["suggestion_set"]["suggestion_set_id"] == command["suggestion_set_id"]
            )
            event = command["event"]
            assert event["session_id"] == frozen["session_id"]
            assert event["context_hash"] == frozen["context_hash"]
            assert event["policy_version"] == frozen["policy_version"]
            assert event["provider_snapshot_id"] == frozen["provider_snapshot_id"]
    event_command_transactions = [
        row
        for row in transactions
        if row["step"].startswith(("preview_candidate_", "dismiss_candidate_", "line_completed_"))
    ]
    assert event_command_transactions
    assert all("Idempotency-Key" not in json.dumps(row) for row in event_command_transactions)


def test_two_runs_use_disjoint_workspace_namespaces(tmp_path, monkeypatch):
    cases, labels = _supported_three_city_cases()
    selected = ([cases[0]], {cases[0]["case_id"]: labels[cases[0]["case_id"]]})
    transport = BuilderFixtureTransport()

    first = _run(tmp_path, monkeypatch, transport, cases=selected)
    second = _run(tmp_path, monkeypatch, transport, cases=selected)

    first_output = _rows(first.run_dir / "product_outputs.jsonl")[0]
    second_output = _rows(second.run_dir / "product_outputs.jsonl")[0]
    assert first.run_id != second.run_id
    assert first_output["workspace_id"] != second_output["workspace_id"]


@pytest.mark.parametrize(
    ("event_failure", "failure_code"),
    [
        ("http", "EVENT_COMMAND_UNAVAILABLE"),
        ("context", "EVENT_COMMAND_FROZEN_CONTEXT_INVALID"),
        ("replay", "EVENT_COMMAND_IDEMPOTENT_REPLAY_FAILED"),
    ],
)
def test_event_command_http_context_or_replay_failure_fails_closed(tmp_path, monkeypatch, event_failure, failure_code):
    cases, labels = _supported_three_city_cases()
    selected = ([cases[0]], {cases[0]["case_id"]: labels[cases[0]["case_id"]]})
    result = _run(
        tmp_path,
        monkeypatch,
        BuilderFixtureTransport(event_failure=event_failure),
        cases=selected,
    )

    assert result.gate["decision"] == "REJECT"
    output = _rows(result.run_dir / "product_outputs.jsonl")[0]
    assert output["failure_code"] == failure_code
    assert output["final_snapshot"]["current_revision"]["revision"] == 1


def test_event_missing_from_ledger_readback_rejects_after_http_command_success(tmp_path, monkeypatch):
    cases, labels = _supported_three_city_cases()
    selected = ([cases[0]], {cases[0]["case_id"]: labels[cases[0]["case_id"]]})
    result = _run(
        tmp_path,
        monkeypatch,
        BuilderFixtureTransport(event_failure="readback"),
        cases=selected,
    )

    assert result.gate["decision"] == "REJECT"
    score = json.loads((result.run_dir / "deterministic_scores.json").read_text(encoding="utf-8"))["cases"][0]
    readback = next(check for check in score["checks"] if check["id"] == "EVENT_COMMANDS_EXACT_LEDGER_READBACK")
    assert readback["status"] == "FAIL"
    assert readback["missing_event_ids"]


@pytest.mark.parametrize("leak", ["wrong_city", "hard", "unknown"])
def test_top3_wrong_city_hard_or_unknown_leak_rejects(tmp_path, monkeypatch, leak):
    cases, labels = _supported_three_city_cases()
    selected = ([cases[0]], {cases[0]["case_id"]: labels[cases[0]["case_id"]]})
    result = _run(tmp_path, monkeypatch, BuilderFixtureTransport(leak=leak), cases=selected)

    assert result.gate["decision"] == "REJECT"
    scores = json.loads((result.run_dir / "deterministic_scores.json").read_text(encoding="utf-8"))["cases"]
    top3 = [check for check in scores[0]["checks"] if "TOP3" in check["id"]]
    assert top3[0]["status"] == "FAIL"


@pytest.mark.parametrize("code", ["SUGGESTION_SET_STALE", "SUGGESTION_SET_EXPIRED", "CANDIDATE_NOT_IN_FROZEN_SET"])
def test_stale_expired_or_tampered_accept_rejects_and_preserves_revision(tmp_path, monkeypatch, code):
    cases, labels = _supported_three_city_cases()
    selected = ([cases[0]], {cases[0]["case_id"]: labels[cases[0]["case_id"]]})
    result = _run(tmp_path, monkeypatch, BuilderFixtureTransport(accept_error=code), cases=selected)

    assert result.gate["decision"] == "REJECT"
    output = _rows(result.run_dir / "product_outputs.jsonl")[0]
    assert output["failure_code"] == code
    assert output["rounds"][0]["rollback_preserved_revision"] is True
    assert output["final_snapshot"]["current_revision"]["revision"] == 1


def test_provider_unavailable_fails_closed_without_receipts(tmp_path, monkeypatch):
    cases, labels = _supported_three_city_cases()
    selected = ([cases[0]], {cases[0]["case_id"]: labels[cases[0]["case_id"]]})
    result = _run(tmp_path, monkeypatch, BuilderFixtureTransport(provider_unavailable=True), cases=selected)

    assert result.gate["decision"] == "REJECT"
    output = _rows(result.run_dir / "product_outputs.jsonl")[0]
    assert output["failure_code"] == "SUGGESTION_PROVIDER_UNAVAILABLE"
    assert _rows(result.run_dir / "provider_receipts.jsonl") == []


def test_product_provider_identity_mismatch_rejects_before_auth_or_case_execution(tmp_path, monkeypatch):
    result = _run(
        tmp_path,
        monkeypatch,
        BuilderFixtureTransport(provider_identity_mismatch=True),
    )

    assert result.gate["decision"] == "REJECT"
    assert result.gate["execution"]["reason"] == "PRODUCT_HTTP_ADAPTER_UNAVAILABLE"
    transactions = _rows(result.run_dir / "http_transactions.jsonl")
    assert [item["step"] for item in transactions] == ["suggestion_provider_handshake"]
    assert _rows(result.run_dir / "product_outputs.jsonl") == []


def test_unavailable_real_localhost_fails_closed_and_records_transport_error(tmp_path, monkeypatch):
    valid = _valid_preflight()
    spec = copy.deepcopy(valid.resolved_spec)
    spec["sut"]["base_url"] = "http://127.0.0.1:1"
    monkeypatch.setattr(builder_module, "preflight", lambda *args, **kwargs: replace(valid, resolved_spec=spec))

    result = run_builder_http(SPEC, runs_root=tmp_path / "runs", timeout_seconds=0.2, environ={})

    assert result.gate["decision"] == "REJECT"
    assert result.gate["execution"]["reason"] == "PRODUCT_HTTP_ADAPTER_UNAVAILABLE"
    transactions = _rows(result.run_dir / "http_transactions.jsonl")
    assert transactions[0]["step"] == "suggestion_provider_handshake"
    assert transactions[0]["status_code"] is None
    assert "transport_error" in transactions[0]


def test_checked_in_g2_g5_cases_only_report_actual_unsupported_boundaries(tmp_path, monkeypatch):
    result = _run(tmp_path, monkeypatch, BuilderFixtureTransport())

    assert result.gate["decision"] == "REJECT"
    outputs = _rows(result.run_dir / "product_outputs.jsonl")
    assert len(outputs) == 7
    unsupported = {row["step"] for output in outputs for row in output["unsupported_capabilities"]}
    assert "restart_backend_yjs" in unsupported
    assert "drag_stop" not in unsupported
    assert "move_stop_button" not in unsupported
    assert "concurrent_edit" not in unsupported
    assert "incremental_audit" not in unsupported
    recovery_outputs = [output for output in outputs if output["recovery_contracts"].get("drag_button_equivalence")]
    assert recovery_outputs
    assert all(
        output["recovery_contracts"]["drag_button_equivalence"]["status"] == "PASS" for output in recovery_outputs
    )
    concurrent_outputs = [output for output in outputs if output["recovery_contracts"].get("concurrent_edit")]
    assert concurrent_outputs
    assert all(output["recovery_contracts"]["concurrent_edit"]["status"] == "PASS" for output in concurrent_outputs)
    scores = json.loads((result.run_dir / "deterministic_scores.json").read_text(encoding="utf-8"))
    selected_case_ids = json.loads(SPEC.read_text(encoding="utf-8"))["dataset"]["case_ids"]
    for metric in ("builder_ndcg_at_5", "builder_recall_at_5"):
        aggregate = scores["metric_aggregate"]["metrics"][metric]
        assert aggregate["applicable_case_ids"] == selected_case_ids
        assert aggregate["coverage"] == {"numerator": 7, "denominator": 7, "value": 1.0}
        gate = next(item for item in scores["metric_gates"] if item["id"] == f"METRIC_THRESHOLD:{metric}")
        # The real-snapshot candidate IDs are not silently remapped to the
        # controlled transport's synthetic IDs. The mismatch remains a real reject.
        assert gate["status"] == "FAIL"
        assert "THRESHOLD_NOT_MET" in gate["reason_codes"]
    assert "preview_candidate" not in unsupported
    assert "dismiss_candidate" not in unsupported
    assert "line_completed" not in unsupported
    undo_outputs = [output for output in outputs if output["undo_attempted"]]
    assert undo_outputs
    assert all(output["undo_revision_after"] == output["undo_revision_before"] + 1 for output in undo_outputs)
    assert all(output["undo_event"]["event_type"] == "stop_undone" for output in undo_outputs)
    assert all(
        output["undo_event"]["payload"]["source_accept_event_id"] == output["undo_source_accept_event"]["event_id"]
        for output in undo_outputs
    )


def test_successful_external_restart_gate_binds_fresh_http_readback_to_restart_cases(tmp_path, monkeypatch):
    def passed_restart(*_args, **_kwargs):
        return RestartGateResult(
            "PASS",
            "RESTART_EVIDENCE_VALID",
            {
                "schema_version": "backend-yjs-restart-gate-v1",
                "status": "PASS",
                "claim_scope": "local_fixture_public_http_yjs_browser",
            },
        )

    result = _run(
        tmp_path,
        monkeypatch,
        BuilderFixtureTransport(),
        restart_gate_runner=passed_restart,
    )

    outputs = _rows(result.run_dir / "product_outputs.jsonl")
    restart_outputs = [
        output
        for output in outputs
        if "restart_backend_yjs"
        in next(case for case in _selected()[0] if case["case_id"] == output["case_id"])["execution"]["steps"]
    ]
    assert restart_outputs
    for output in restart_outputs:
        assert not any(item["step"] == "restart_backend_yjs" for item in output["unsupported_capabilities"])
        contract = output["recovery_contracts"]["backend_yjs_restart"]
        assert contract["status"] == "PASS"
        assert contract["revision_exact"] is True
        assert contract["events_exact"] is True
    assert json.loads((result.run_dir / "restart_gate.json").read_text(encoding="utf-8"))["status"] == "PASS"
