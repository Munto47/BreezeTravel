"""Execute an exact P5 variant run group without reading any oracle bundle."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from evals.trip_check_v1.p5.adapters import ADAPTERS
from evals.trip_check_v1.p5.contracts import P5TerminalOutput, P5VariantRunSpec
from evals.trip_check_v1.p5.data_contract import digest
from evals.trip_check_v1.p5.runner import (
    execute_terminal,
    validate_exact_terminal_set,
    write_jsonl_atomic,
)


BACKEND_ROOT = Path(__file__).resolve().parents[1]
P5_ROOT = BACKEND_ROOT / "evals" / "trip_check_v1" / "p5"
REPO_ROOT = BACKEND_ROOT.parent
DEFAULT_EVIDENCE_ROOT = BACKEND_ROOT / "evidence" / "trip_check_v1" / "p5"
VARIANT_ALIASES = {
    "legacy": "legacy_a",
    "legacy_a": "legacy_a",
    "core": "core_b",
    "core_b": "core_b",
    "solver": "solver_c",
    "solver_c": "solver_c",
}


def _git(*args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    return completed.stdout.strip()


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_cases(lane: str) -> list[dict[str, Any]]:
    path = (
        P5_ROOT / "cases_nonblind_v1.jsonl"
        if lane == "nonblind"
        else P5_ROOT / "frozen_blind.inputs.jsonl"
    )
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run_spec(
    *,
    lane: str,
    subject_commit: str,
    dirty_tree: bool,
    case_set_hash: str,
    variant_id: str,
) -> P5VariantRunSpec:
    manifest = _load_json(P5_ROOT / "dataset_v1.manifest.json")
    template_path = P5_ROOT / "run_spec_template_v1.json"
    template = _load_json(template_path)
    variant = template["variant_specs"][variant_id]
    return P5VariantRunSpec(
        subject_commit=subject_commit,
        dirty_tree=dirty_tree,
        lane=lane,
        dataset_manifest_hash=manifest["manifest_hash"],
        case_set_hash=case_set_hash,
        run_spec_template_hash=_sha256_file(template_path),
        provider_snapshot_id=template["provider_snapshot_id"],
        execution_mode=template["execution_mode"],
        random_seed=template["random_seed"],
        budget=template["budget"],
        replay_hash_policy=template["replay_hash_policy"],
        variant_id=variant_id,
        adapter_version=variant["adapter_version"],
        repair_strategy=variant["repair_strategy"],
    )


async def _execute(args: argparse.Namespace) -> dict[str, Any]:
    subject_commit = _git("rev-parse", "HEAD")
    dirty_tree = bool(_git("status", "--short"))
    if dirty_tree and not args.allow_dirty:
        raise RuntimeError("working tree is dirty; use --allow-dirty only for development runs")
    cases = _load_cases(args.lane)
    if args.case_id:
        requested = set(args.case_id)
        cases = [case for case in cases if case["case_id"] in requested]
        missing = requested - {case["case_id"] for case in cases}
        if missing:
            raise ValueError(f"unknown case IDs: {sorted(missing)}")
    if args.limit is not None:
        cases = cases[: args.limit]
    if not cases:
        raise ValueError("selected case set is empty")
    variant_ids = []
    for value in args.variants.split(","):
        key = value.strip()
        if key not in VARIANT_ALIASES:
            raise ValueError(f"unknown variant: {key}")
        variant_id = VARIANT_ALIASES[key]
        if variant_id not in variant_ids:
            variant_ids.append(variant_id)
    case_ids = {case["case_id"] for case in cases}
    case_set_hash = digest(sorted(case_ids))
    outputs: list[P5TerminalOutput] = []
    run_specs = {}
    replay_mismatches = []
    for variant_id in variant_ids:
        spec = _run_spec(
            lane=args.lane,
            subject_commit=subject_commit,
            dirty_tree=dirty_tree,
            case_set_hash=case_set_hash,
            variant_id=variant_id,
        )
        run_specs[variant_id] = spec.model_dump(mode="json")
        adapter = ADAPTERS[variant_id]()
        for case in cases:
            output = await execute_terminal(case=case, run_spec=spec, adapter=adapter)
            outputs.append(output)
            if args.replay:
                replayed = await execute_terminal(case=case, run_spec=spec, adapter=adapter)
                if replayed.replay_hash != output.replay_hash:
                    replay_mismatches.append(
                        {
                            "case_id": case["case_id"],
                            "variant_id": variant_id,
                            "first": output.replay_hash,
                            "second": replayed.replay_hash,
                        }
                    )
    validate_exact_terminal_set(
        outputs,
        case_ids=case_ids,
        variant_ids=set(variant_ids),
    )
    output_root = Path(args.output_dir).resolve() if args.output_dir else DEFAULT_EVIDENCE_ROOT
    run_id = args.run_id or datetime.now(timezone.utc).strftime("p5-%Y%m%dT%H%M%SZ")
    run_dir = output_root / run_id
    terminal_path = run_dir / "terminal_outputs.jsonl"
    output_content_hash = write_jsonl_atomic(terminal_path, outputs)
    terminal_file_hash = _sha256_file(terminal_path)
    manifest = {
        "schema_version": "trip-check-p5-run-group-v1",
        "run_id": run_id,
        "status": "PASS" if not replay_mismatches else "REJECT",
        "formal_evidence": not dirty_tree and args.limit is None and not args.case_id,
        "lane": args.lane,
        "subject_commit": subject_commit,
        "dirty_tree": dirty_tree,
        "case_count": len(cases),
        "case_set_hash": case_set_hash,
        "variant_ids": variant_ids,
        "variant_count": len(variant_ids),
        "terminal_count": len(outputs),
        "expected_terminal_count": len(cases) * len(variant_ids),
        "run_specs": run_specs,
        "terminal_outputs_path": terminal_path.name,
        "terminal_outputs_file_sha256": terminal_file_hash,
        "terminal_outputs_content_sha256": output_content_hash,
        "replay_executed": args.replay,
        "replay_match_count": len(outputs) - len(replay_mismatches) if args.replay else 0,
        "replay_mismatches": replay_mismatches,
        "blind_labels_read": False,
        "external_api_calls": 0,
        "human_evidence": False,
    }
    manifest["manifest_hash"] = digest(manifest)
    manifest_path = run_dir / "run_group_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return {**manifest, "run_dir": str(run_dir)}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lane", choices=("nonblind", "frozen-blind", "frozen_blind"), required=True)
    parser.add_argument("--variants", default="legacy,core,solver")
    parser.add_argument("--case-id", action="append")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--replay", action="store_true")
    parser.add_argument("--allow-dirty", action="store_true")
    parser.add_argument("--run-id")
    parser.add_argument("--output-dir")
    return parser


def main() -> None:
    args = _parser().parse_args()
    if args.lane == "frozen-blind":
        args.lane = "frozen_blind"
    result = asyncio.run(_execute(args))
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

