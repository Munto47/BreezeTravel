"""Run, wrap, aggregate, and validate real P5 v4 formal receipts."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from evals.trip_check_v1.p5.formal_receipts_v4 import (  # noqa: E402
    COMMAND_KINDS_V4,
    VERIFICATION_KINDS_V4,
    build_dataset_formal_validation_receipt_v4,
    build_formal_gate_receipt_v4,
    build_verification_receipt_v4,
    execute_command_receipt_v4,
    validate_command_result_v4,
    validate_dataset_formal_validation_receipt_v4,
    validate_formal_gate_receipt_v4,
    validate_verification_receipt_v4,
)


REPO_ROOT = BACKEND_ROOT.parent
P5_ROOT = REPO_ROOT / "backend" / "evals" / "trip_check_v1" / "p5"


def _named_paths(values: list[str], flag: str) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for value in values:
        if "=" not in value:
            raise SystemExit(f"{flag} must use NAME=ABSOLUTE_PATH")
        name, raw_path = value.split("=", 1)
        path = Path(raw_path)
        if not name or name in result or not path.is_absolute():
            raise SystemExit(f"{flag} must use unique NAME=ABSOLUTE_PATH")
        result[name] = path
    return result


def _primary(args: argparse.Namespace) -> dict[str, Path]:
    return {
        "dataset_manifest": args.dataset,
        "active_contract": args.active_contract,
        "blind_seal": args.blind_seal,
        "run_spec": args.run_spec,
        "judge_rubric": args.rubric,
        "nonblind_run_manifest": args.nonblind_run_manifest,
        "nonblind_score": args.nonblind_score,
        "blind_run_manifest": args.blind_run_manifest,
        "blind_score": args.blind_score,
        "judge_panel": args.judge_panel,
    }


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="action", required=True)

    run = commands.add_parser("run", help="Execute and record one real command.")
    run.add_argument("--kind", choices=COMMAND_KINDS_V4, required=True)
    run.add_argument("--cwd", type=Path, required=True)
    run.add_argument("--config-artifact", action="append", default=[])
    run.add_argument("--expected-artifact", action="append", default=[])
    run.add_argument("--output-dir", type=Path, required=True)
    run.add_argument("command", nargs=argparse.REMAINDER)

    dataset = commands.add_parser("dataset", help="Build formal dataset receipt.")
    dataset.add_argument("--command-result", type=Path, required=True)
    dataset.add_argument(
        "--dataset", type=Path, default=P5_ROOT / "dataset_v4.manifest.json"
    )
    dataset.add_argument(
        "--validator",
        type=Path,
        default=REPO_ROOT / "backend" / "scripts" / "validate_trip_check_p5_dataset_v4.py",
    )
    dataset.add_argument("--output", type=Path, required=True)

    verification = commands.add_parser(
        "verification", help="Wrap one recorded P1-P4/tool command."
    )
    verification.add_argument("--command-result", type=Path, required=True)
    verification.add_argument("--output", type=Path, required=True)

    formal = commands.add_parser("formal", help="Aggregate the Gate formal receipt.")
    formal.add_argument("--dataset-validation-receipt", type=Path, required=True)
    formal.add_argument(
        "--verification",
        action="append",
        default=[],
        help="Repeat KIND=ABSOLUTE_PATH for all eight required kinds.",
    )
    formal.add_argument(
        "--dataset", type=Path, default=P5_ROOT / "dataset_v4.manifest.json"
    )
    formal.add_argument(
        "--active-contract", type=Path, default=P5_ROOT / "active_contract.json"
    )
    formal.add_argument(
        "--blind-seal",
        type=Path,
        default=P5_ROOT / "sealed" / "frozen_blind.v4.seal.json",
    )
    formal.add_argument(
        "--run-spec", type=Path, default=P5_ROOT / "run_spec_template_v4.json"
    )
    formal.add_argument(
        "--rubric", type=Path, default=P5_ROOT / "judge_rubric_v2.json"
    )
    formal.add_argument("--nonblind-run-manifest", type=Path, required=True)
    formal.add_argument("--nonblind-score", type=Path, required=True)
    formal.add_argument("--blind-run-manifest", type=Path, required=True)
    formal.add_argument("--blind-score", type=Path, required=True)
    formal.add_argument("--judge-panel", type=Path, required=True)
    formal.add_argument("--output", type=Path, required=True)

    validate = commands.add_parser("validate", help="Read back a generated receipt.")
    validate.add_argument(
        "--type", choices=("command", "dataset", "verification", "formal"), required=True
    )
    validate.add_argument("--receipt", type=Path, required=True)
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.action == "run":
        command = list(args.command)
        if command[:1] == ["--"]:
            command = command[1:]
        receipt = execute_command_receipt_v4(
            repo_root=REPO_ROOT,
            kind=args.kind,
            command=command,
            command_cwd=args.cwd,
            config_artifacts=_named_paths(args.config_artifact, "--config-artifact"),
            expected_artifacts=_named_paths(
                args.expected_artifact, "--expected-artifact"
            ),
            output_dir=args.output_dir,
        )
    elif args.action == "dataset":
        receipt = build_dataset_formal_validation_receipt_v4(
            repo_root=REPO_ROOT,
            command_result_path=args.command_result,
            dataset_manifest_path=args.dataset,
            validator_path=args.validator,
            output_path=args.output,
        )
    elif args.action == "verification":
        receipt = build_verification_receipt_v4(
            repo_root=REPO_ROOT,
            command_result_path=args.command_result,
            output_path=args.output,
        )
    elif args.action == "formal":
        verifications = _named_paths(args.verification, "--verification")
        if set(verifications) != set(VERIFICATION_KINDS_V4):
            raise SystemExit("--verification must bind exactly the eight required kinds")
        receipt = build_formal_gate_receipt_v4(
            repo_root=REPO_ROOT,
            dataset_receipt_path=args.dataset_validation_receipt,
            verification_receipts=verifications,
            primary_artifacts=_primary(args),
            output_path=args.output,
        )
    else:
        validators = {
            "command": validate_command_result_v4,
            "dataset": validate_dataset_formal_validation_receipt_v4,
            "verification": validate_verification_receipt_v4,
            "formal": validate_formal_gate_receipt_v4,
        }
        receipt = validators[args.type](args.receipt)
    print(
        json.dumps(
            {
                "schema_version": receipt["schema_version"],
                "status": receipt["status"],
                "receipt_hash": receipt.get("receipt_hash"),
                "receipt_path": receipt.get("receipt_path"),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if receipt["status"] in {"PASS", "MINTED_NOT_CONSUMED"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
