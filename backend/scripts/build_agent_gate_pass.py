from __future__ import annotations

import argparse
from pathlib import Path
from typing import cast

from evals.agent_gate_v1.candidate_gate import verify_g07_candidate_gate_pass
from evals.agent_gate_v1.contracts import HardeningControl
from evals.agent_gate_v1.core_gate import (
    read_worktree_binding,
    verify_core_agent_gate_pass,
)
from evals.agent_gate_v1.scope_guard import ScopeGuardError, validate_mainline_scope


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Build a CORE or G07 Gate pass from repository-external evidence "
            "without reading private signing keys."
        )
    )
    parser.add_argument("--component", action="append", type=Path)
    parser.add_argument("--development-checkout", required=True, type=Path)
    parser.add_argument("--candidate-commit", required=True)
    parser.add_argument("--candidate-tree", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--automated-manifest-output", type=Path)
    parser.add_argument("--live-score", type=Path)
    parser.add_argument("--panel-verification", type=Path)
    parser.add_argument("--sealed-receipt", type=Path)
    parser.add_argument("--sealed-score-receipt", type=Path)
    parser.add_argument("--hardening-decision", type=Path)
    parser.add_argument(
        "--hardening-control",
        action="append",
        help="selected G07 control receipt as CONTROL=PATH",
    )
    args = parser.parse_args()
    try:
        scope_report = validate_mainline_scope(
            REPOSITORY_ROOT,
            requested_phase="GATE_RUNNING",
            expected_candidate_commit=args.candidate_commit,
        )
    except ScopeGuardError as exc:
        parser.error(str(exc))
    if scope_report.verdict != "PASS":
        parser.error(
            "scope guard rejected Gate entry: "
            f"{scope_report.verdict} {scope_report.error_codes}"
        )
    binding = read_worktree_binding(REPOSITORY_ROOT)
    if binding.gate_profile == "CORE_AGENT_GATE":
        if args.component:
            parser.error("CORE_AGENT_GATE does not accept signed HARDENED components")
        core_paths = (
            args.automated_manifest_output,
            args.live_score,
            args.panel_verification,
            args.sealed_receipt,
            args.sealed_score_receipt,
        )
        if any(path is None for path in core_paths):
            parser.error(
                "CORE_AGENT_GATE requires automated, live, panel, and sealed receipts"
            )
        verify_core_agent_gate_pass(
            repository_root=REPOSITORY_ROOT,
            development_checkout_root=args.development_checkout,
            expected_candidate_commit=args.candidate_commit,
            expected_candidate_tree=args.candidate_tree,
            automated_manifest_output=args.automated_manifest_output,
            live_score_path=args.live_score,
            panel_verification_path=args.panel_verification,
            sealed_receipt_path=args.sealed_receipt,
            sealed_score_receipt_path=args.sealed_score_receipt,
            output_path=args.output,
        )
        return 0
    if binding.goal_sequence != 7:
        parser.error("HARDENED_CANDIDATE_GATE is restricted to G07")
    if not args.component or len(args.component) != 4:
        parser.error("G07 candidate Gate requires four candidate-bound components")
    if args.hardening_decision is None:
        parser.error("G07 candidate Gate requires a hardening decision receipt")
    controls: dict[HardeningControl, Path] = {}
    allowed_controls = {
        "EXTERNAL_AUTHORITY",
        "PURPOSE_SPECIFIC_BROKER",
        "ROLE_SIGNATURES",
        "IMMUTABLE_REMOTE_REF",
        "ISOLATED_OCI",
    }
    for raw in args.hardening_control or []:
        control, separator, raw_path = raw.partition("=")
        if not separator or control not in allowed_controls or not raw_path:
            parser.error("--hardening-control must use a known CONTROL=PATH value")
        typed_control = cast(HardeningControl, control)
        if typed_control in controls:
            parser.error("duplicate --hardening-control value")
        controls[typed_control] = Path(raw_path)
    verify_g07_candidate_gate_pass(
        repository_root=REPOSITORY_ROOT,
        development_checkout_root=args.development_checkout,
        expected_candidate_commit=args.candidate_commit,
        expected_candidate_tree=args.candidate_tree,
        component_receipt_paths=args.component,
        hardening_decision_path=args.hardening_decision,
        hardening_control_receipt_paths=controls,
        output_path=args.output,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
