from __future__ import annotations

import asyncio
import json

from evals.trip_check_v1.pilot_runner import run_pilot


def test_p1_pilot_runner_executes_all_cases_and_writes_bound_artifacts(tmp_path):
    output = tmp_path / "p1"
    result = asyncio.run(run_pilot(commit_sha="05f40bf", output_dir=output))

    assert result["status"] == "PASS"
    assert result["metrics"]["case_count"] == 18
    assert result["metrics"]["city_counts"] == {"上海": 6, "北京": 6, "杭州": 6}
    assert result["metrics"]["wrong_poi_auto_accept_count"] == 0
    assert result["metrics"]["repair_new_high_count"] == 0
    assert result["metrics"]["repair_new_unknown_count"] == 0
    assert result["metrics"]["resolution_required_count"] == 6
    assert result["metrics"]["succeeded_run_count"] >= 3
    results = [json.loads(line) for line in (output / "results.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(results) == 18
    assert all(item["terminal_state"] != "NOT_RUN" for item in results)
    assert {
        item["case_id"]
        for item in results
        if item["case_id"] in {"TC-P1-BJ-01", "TC-P1-SH-01", "TC-P1-HZ-01"}
        and item["terminal_state"] == "SUCCEEDED"
    } == {"TC-P1-BJ-01", "TC-P1-SH-01", "TC-P1-HZ-01"}
    manifest = json.loads((output / "pilot_manifest.json").read_text(encoding="utf-8"))
    assert manifest["commit_sha"] == "05f40bf"
    assert manifest["evidence_class"] == "CONTROLLED_FIXTURE"
    assert manifest["human_evidence"] is False
