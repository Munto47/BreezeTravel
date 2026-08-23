from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from unittest.mock import patch

import pytest

from evals.trip_check_v1.p5.contracts_v2 import (
    P5CaseV2,
    P5TerminalOutputV2,
    P5VariantRunSpecV2,
    TerminalStatusV2,
)
from evals.trip_check_v1.p5.runner_v2 import (
    execute_terminal_v2,
    validate_exact_terminal_set_v2,
    validate_run_spec_whitelist_v2,
)
from evals.trip_check_v1.p5.scorer_v2 import (
    P5V2ScoringError,
    build_score_report_v2,
    score_run_group_v2,
)
from scripts import run_trip_check_p5_v2_eval as run_cli
from tests.p5_v2_e2e_helpers import (
    DATASET_MANIFEST_PATH,
    NONBLIND_CASES_PATH,
    NONBLIND_MATERIALIZATIONS_PATH,
    load_jsonl,
)


pytestmark = pytest.mark.integration


@dataclass(frozen=True)
class PilotRun:
    result: dict
    run_dir: Path
    outputs: list[P5TerminalOutputV2]
    cases: list[P5CaseV2]
    materializations: dict[str, dict]
    elapsed_seconds: float


@pytest.fixture(scope="module")
def pilot_abc_replay(tmp_path_factory: pytest.TempPathFactory) -> PilotRun:
    case_rows = load_jsonl(NONBLIND_CASES_PATH)
    pilot_rows = [row for row in case_rows if row["split"] == "pilot"]
    case_ids = [row["case_id"] for row in pilot_rows]
    output_root = tmp_path_factory.mktemp("p5-v2-pilot")
    argv = [
        "--lane",
        "nonblind",
        "--replay",
        "--allow-dirty",
        "--run-id",
        "pilot-abc-replay",
        "--output-dir",
        str(output_root),
    ]
    for case_id in case_ids:
        argv.extend(("--case-id", case_id))

    subject_commit = run_cli._git("rev-parse", "HEAD")

    def clean_git(*args: str) -> str:
        return subject_commit if args == ("rev-parse", "HEAD") else ""

    started = perf_counter()
    with patch.object(run_cli, "_git", side_effect=clean_git):
        result = asyncio.run(run_cli.execute_run(run_cli.parser().parse_args(argv)))
    elapsed = perf_counter() - started
    run_dir = Path(result["run_dir"])
    outputs = [
        P5TerminalOutputV2.model_validate(json.loads(line))
        for line in (run_dir / "terminal_outputs.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    materializations = {
        row["case_id"]: row for row in load_jsonl(NONBLIND_MATERIALIZATIONS_PATH)
    }
    return PilotRun(
        result=result,
        run_dir=run_dir,
        outputs=outputs,
        cases=[P5CaseV2.model_validate(row) for row in pilot_rows],
        materializations=materializations,
        elapsed_seconds=elapsed,
    )


def test_pilot_18_by_abc_has_exact_terminals_and_full_replay(pilot_abc_replay: PilotRun) -> None:
    run = pilot_abc_replay
    assert run.result["status"] == "PASS"
    assert run.result["formal_evidence"] is False
    assert run.result["case_count"] == 18
    assert run.result["variant_ids"] == ["legacy_a", "core_b", "solver_c"]
    assert run.result["terminal_count"] == run.result["expected_terminal_count"] == 54
    assert run.result["replay_executed"] is True
    assert run.result["replay_match_count"] == 54
    assert run.result["replay_mismatches"] == []
    validate_exact_terminal_set_v2(
        run.outputs,
        case_ids={case.case_id for case in run.cases},
        variant_ids={"legacy_a", "core_b", "solver_c"},
    )


def test_pilot_run_specs_only_differ_on_the_frozen_whitelist(
    pilot_abc_replay: PilotRun,
) -> None:
    specs = [
        P5VariantRunSpecV2.model_validate(value)
        for value in pilot_abc_replay.result["run_specs"].values()
    ]
    validate_run_spec_whitelist_v2(specs)
    tampered = specs[1].model_copy(
        update={"budget": {**specs[1].budget, "timeout_seconds": 31}}
    )
    with pytest.raises(ValueError, match="outside the P5 variant whitelist"):
        validate_run_spec_whitelist_v2([specs[0], tampered])


def test_pilot_readback_scores_all_terminal_rows_without_claiming_promotion(
    pilot_abc_replay: PilotRun,
) -> None:
    run = pilot_abc_replay
    report = build_score_report_v2(
        manifest=run.result,
        cases=run.cases,
        outputs=run.outputs,
        include_case_scores=True,
    )
    assert report["schema_version"] == "trip-check-p5-nonblind-score-report-v2"
    assert report["case_count"] == 18
    assert report["terminal_count"] == 54
    assert len(report["case_scores"]) == 54
    assert report["automated_proxy_judge"] == "NOT_RUN"
    assert report["live_provider_evidence"] is False
    assert report["public_e2e_evidence"] is False
    assert report["human_evidence"] is False
    assert report["report_hash"]


@pytest.mark.xfail(
    strict=True,
    raises=P5V2ScoringError,
    reason="partial runner binds full files while scorer requires those files to equal the selected 18",
)
def test_partial_pilot_run_group_can_be_read_back_by_scorer(
    pilot_abc_replay: PilotRun,
) -> None:
    try:
        report = score_run_group_v2(
            run_dir=pilot_abc_replay.run_dir,
            cases_path=NONBLIND_CASES_PATH,
            materializations_path=NONBLIND_MATERIALIZATIONS_PATH,
            dataset_manifest_path=DATASET_MANIFEST_PATH,
            require_formal=False,
        )
    except P5V2ScoringError as exc:
        if str(exc) != "RUN_GROUP_CASE_SET_BINDING_MISMATCH":
            raise AssertionError(f"unexpected scorer rejection: {exc}") from exc
        raise
    assert report["case_count"] == 18


def test_pilot_performance_and_zero_api_contract(pilot_abc_replay: PilotRun) -> None:
    run = pilot_abc_replay
    assert run.elapsed_seconds < 60
    assert run.result["external_api_calls"] == 0
    assert run.result["blind_labels_read"] is False
    assert run.result["human_evidence"] is False
    assert all(output.capability_outcomes["external_api_calls"] == "0" for output in run.outputs)
    assert all(output.token_count == 0 and output.cost_usd == 0 for output in run.outputs)


class _ExplodingAdapter:
    variant_id = "core_b"
    adapter_version = "core-b-v2"
    repair_strategy = "bounded_repair_v1"

    async def execute(self, *_args, **_kwargs):
        raise RuntimeError("controlled adapter failure")


def test_adapter_exception_is_a_hash_bound_terminal_row(pilot_abc_replay: PilotRun) -> None:
    run = pilot_abc_replay
    case = run.cases[0]
    spec = P5VariantRunSpecV2.model_validate(run.result["run_specs"]["core_b"])
    output = asyncio.run(
        execute_terminal_v2(
            case=case,
            materialization=run.materializations[case.case_id],
            run_spec=spec,
            adapter=_ExplodingAdapter(),
        )
    )
    assert output.terminal_status is TerminalStatusV2.ERROR
    assert output.error_category == "RuntimeError"
    assert output.receipts == [{"type": "runner_error", "category": "RuntimeError"}]
    assert output.semantic_output_hash == output.replay_hash
    validate_exact_terminal_set_v2(
        [output], case_ids={case.case_id}, variant_ids={"core_b"}
    )
