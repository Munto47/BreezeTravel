from scripts.run_trip_check_p4_solver_bakeoff import run_bakeoff


def test_p4_solver_bakeoff_writes_replayable_fail_closed_manifest(tmp_path):
    manifest = run_bakeoff(commit_sha="142fa30", output_dir=tmp_path)

    assert manifest["case_count"] == 36
    assert manifest["record_count"] == 108
    assert manifest["admission"]["safety_pass"] is True
    assert manifest["admission"]["authoritative_postcheck_pass"] is True
    assert manifest["admission"]["replay_pass"] is True
    assert manifest["admission"]["performance_pass"] is True
    assert manifest["admission"]["default_strategy"] in {
        "bounded_repair_v1", "cp_sat_v1"
    }
    assert manifest["evidence_boundary"]["human_evidence"] == "NOT_RUN"
    assert (tmp_path / "strategy_receipts.jsonl").exists()
    assert (tmp_path / "solver_bakeoff_manifest.json").exists()
