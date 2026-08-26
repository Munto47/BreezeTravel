from __future__ import annotations

from pathlib import Path

import pytest

from evals.trip_check_v1.p6.contracts_v1 import P6ContractError
from evals.trip_check_v1.p6.public_e2e_runner import _execute_public_flow, run_public_e2e
from tests.test_trip_check_p6_performance_runner import _spec


def _flow(spec, credentials):  # noqa: ANN001, ANN202
    assert spec["subject_commit"] == "1" * 40
    assert set(credentials) == {"P6_PUBLIC_TEST_EMAIL", "P6_PUBLIC_TEST_PASSWORD"}
    return {
        "health_http_status": 200,
        "health_response_body_sha256": "2" * 64,
        "step_count": 16,
        "step_set_sha256": "3" * 64,
        "step_receipt_set_sha256": "4" * 64,
        "final_resume_body_sha256": "5" * 64,
        "initial_revision": 1,
        "final_revision": 2,
        "advice_action_count": 1,
        "repair_count": 1,
        "provider_failure_count": 0,
        "postcheck_status": "SUCCEEDED",
        "controlled_snapshot": True,
    }


def test_public_e2e_runner_emits_bound_health_and_chain_receipts(tmp_path: Path) -> None:
    spec_path, _ = _spec(tmp_path)
    output = tmp_path / "public"
    health, e2e = run_public_e2e(
        candidate_run_spec_path=spec_path,
        output_root=output,
        repo_root=Path(__file__).parents[2],
        formal=False,
        flow_runner=_flow,
    )
    assert health["status"] == "PASS"
    assert e2e["status"] == "PASS"
    assert (output / "public_full_chain_readback.json").is_file()
    assert (output / "public_health_receipt.json").is_file()
    assert (output / "public_e2e_receipt.json").is_file()


def test_public_e2e_runner_rejects_missing_postcheck(tmp_path: Path) -> None:
    spec_path, _ = _spec(tmp_path)

    def failed(spec, credentials):  # noqa: ANN001, ANN202
        value = _flow(spec, credentials)
        value["postcheck_status"] = "WAITING"
        return value

    with pytest.raises(P6ContractError, match="P6_G5_PUBLIC_FLOW_INVALID"):
        run_public_e2e(
            candidate_run_spec_path=spec_path,
            output_root=tmp_path / "public",
            repo_root=Path(__file__).parents[2],
            formal=False,
            flow_runner=failed,
        )


def test_public_flow_uses_the_confirmed_brief_revision(monkeypatch) -> None:  # noqa: ANN001
    class FakeClient:
        def __init__(self, base_url: str) -> None:
            assert base_url == "https://example.test"
            self.token = None
            self.steps = []
            self.login_count = 0
            self.resume_count = 0

        def request(  # noqa: ANN201, PLR0911
            self,
            method: str,
            route: str,
            *,
            body=None,  # noqa: ANN001
            headers=None,  # noqa: ANN001
            expected=(200,),  # noqa: ANN001
            record: bool = True,
        ):
            del headers, expected
            status = 200
            if route == "/health":
                value = {"status": "ok"}
            elif route == "/api/auth/email-login":
                self.login_count += 1
                status = 401 if self.login_count == 1 else 200
                value = {} if status == 401 else {"token": "token"}
            elif route == "/api/auth/email-register":
                value = {"user_id": "user"}
            elif route == "/api/room":
                value = {"room_id": "room"}
            elif route == "/api/trip-workspaces":
                status = 201
                value = {"workspace_id": "workspace"}
            elif route.endswith("/imports"):
                status = 201
                value = {"status": "READY", "import_id": "import", "state_version": 1}
            elif route.endswith("/resume"):
                self.resume_count += 1
                value = (
                    {"current_brief": {"revision": 1}}
                    if self.resume_count == 1
                    else {
                        "current_revision": {"revision": 2},
                        "current_trip_check_run": {"stage": "POSTCHECK", "status": "SUCCEEDED"},
                    }
                )
            elif route.endswith("/trip-briefs/1/confirm"):
                value = {"status": "CONFIRMED", "revision": 2}
            elif route.endswith("/imports/import/apply"):
                value = {"revision": {"revision": 1}}
            elif route.endswith("/trip-check-runs"):
                assert body["brief_revision"] == 2
                status = 201
                value = {"run_id": "run"}
            elif route == "/api/trip-check-runs/run":
                value = {
                    "status": "WAITING",
                    "stage": "WAIT_ADOPTION",
                    "report_id": "report",
                    "advice_bundle_id": "advice",
                    "partial_failures": [],
                }
            elif route == "/api/audits/report":
                value = {"overall_status": "VIOLATED"}
            elif route == "/api/audits/report/evidence":
                value = {"provider_failures": []}
            elif route.endswith("/reports/report/advice"):
                value = {"actions": [{"action_id": "action", "repair_id": "advice-repair"}]}
            elif route == "/api/audits/report/repairs":
                value = [
                    {"repair_id": "unlinked-repair", "base_itinerary_revision": 1},
                    {"repair_id": "advice-repair", "base_itinerary_revision": 1},
                ]
            elif route == "/api/audits/report/repairs/advice-repair/apply":
                value = {"postcheck_report_id": "postcheck", "new_revision": 2}
            elif route == "/api/audits/postcheck":
                value = {"itinerary_revision": 2}
            else:
                raise AssertionError(f"unexpected request: {method} {route}")
            if record:
                self.steps.append(
                    {"method": method, "route": route, "status": status, "body_sha256": "a" * 64}
                )
            return value, "a" * 64, status

    monkeypatch.setattr(
        "evals.trip_check_v1.p6.public_e2e_runner._HttpClient",
        FakeClient,
    )
    result = _execute_public_flow(
        {
            "subject_commit": "1" * 40,
            "public_candidate": {"base_url": "https://example.test"},
            "bindings": {
                "ocr_dataset_manifest_sha256": "2" * 64,
                "snapshot_manifest_sha256": "3" * 64,
            },
        },
        {
            "P6_PUBLIC_TEST_EMAIL": "e2e@example.org",
            "P6_PUBLIC_TEST_PASSWORD": "password-1",
        },
    )
    assert result["initial_revision"] == 1
    assert result["final_revision"] == 2
