from __future__ import annotations

import argparse
from pathlib import Path

from evals.agent_gate_v1.core_gate import (
    read_worktree_binding,
    verify_core_agent_gate_pass,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

def main() -> int:
    parser = argparse.ArgumentParser()
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
    args = parser.parse_args()
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
        parser.error("HARDENED_CANDIDATE_GATE requires four signed components")
    parser.error(
        "HARDENED final-Gate signing is fail-closed until an activated "
        "repository-external signer IPC must sign without exposing a key path "
        "to the candidate process"
    )


if __name__ == "__main__":
    raise SystemExit(main())
