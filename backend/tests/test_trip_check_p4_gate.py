from scripts.run_trip_check_p4_gate import evaluate_p4_phase


def _check(status="PASS"):
    return {"name": "check", "status": status}


def test_p4_phase_can_pass_while_cp_sat_admission_is_rejected():
    status = evaluate_p4_phase(
        required_checks=[_check()],
        postgres_status="PASS",
        p2_status="PASS",
        contracts={
            "solver_experiment_complete": True,
            "failed_solver_not_promoted": True,
        },
        sensitive_scan_status="PASS",
    )

    assert status == "PASS"


def test_p4_phase_rejects_missing_postgres_or_contract():
    assert evaluate_p4_phase(
        required_checks=[_check()],
        postgres_status="NOT_RUN",
        p2_status="NOT_RUN",
        contracts={"solver_experiment_complete": True},
        sensitive_scan_status="PASS",
    ) == "REJECT"
    assert evaluate_p4_phase(
        required_checks=[_check()],
        postgres_status="PASS",
        p2_status="PASS",
        contracts={"solver_experiment_complete": False},
        sensitive_scan_status="PASS",
    ) == "REJECT"
