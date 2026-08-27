from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from scripts.run_trip_intake_deepseek_e2e import HttpResult, _poll_run


@dataclass
class SequencedClient:
    bodies: list[dict[str, Any]]
    calls: int = 0

    def request(self, method: str, route: str, *, record: bool) -> HttpResult:
        assert method == "GET"
        assert route == "/api/trip-check-runs/run-1"
        assert record is False
        body = self.bodies[self.calls]
        self.calls += 1
        return HttpResult(body=body, status=200, body_sha256="0" * 64, headers={})


def test_poll_run_does_not_treat_intermediate_waiting_or_partial_as_terminal(monkeypatch) -> None:
    monkeypatch.setattr("scripts.run_trip_intake_deepseek_e2e.time.sleep", lambda _: None)
    client = SequencedClient(
        [
            {"status": "WAITING", "stage": "COLLECT_EVIDENCE", "report_id": None},
            {"status": "PARTIAL", "stage": "AUDIT", "report_id": None},
            {"status": "PARTIAL", "stage": "WAIT_ADOPTION", "report_id": "report-1"},
        ]
    )

    result = _poll_run(client, "run-1")

    assert result["report_id"] == "report-1"
    assert client.calls == 3


def test_poll_run_returns_failed_state_without_a_report(monkeypatch) -> None:
    monkeypatch.setattr("scripts.run_trip_intake_deepseek_e2e.time.sleep", lambda _: None)
    client = SequencedClient(
        [{"status": "FAILED", "stage": "COLLECT_EVIDENCE", "report_id": None}]
    )

    result = _poll_run(client, "run-1")

    assert result["status"] == "FAILED"
    assert client.calls == 1
