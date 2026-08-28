from __future__ import annotations

import argparse
from pathlib import Path

from evals.agent_gate_v1.authority import load_worktree_current_goal_binding
from evals.trip_text_cards_v1.contracts import canonical_sha256


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def tranche_commitment_sha256(
    *,
    input_bundle_sha256: str,
    case_set_commitment_sha256: str,
    truth_bundle_commitment: dict[str, str],
) -> str:
    """Frozen public formula used before a one-shot tranche is minted."""

    return canonical_sha256(
        {
            "input_bundle_sha256": input_bundle_sha256,
            "case_set_commitment_sha256": case_set_commitment_sha256,
            "truth_bundle_commitment": truth_bundle_commitment,
        }
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "G01 sealed scoring remains unavailable until the repository-external "
            "custodian scorer and signer IPC are activation-bound."
        )
    )
    parser.add_argument("--inputs", required=True, type=Path)
    parser.add_argument("--truth", required=True, type=Path)
    parser.add_argument("--predictions", required=True, type=Path)
    parser.add_argument("--inference-outputs", required=True, type=Path)
    parser.add_argument("--prediction-envelope", required=True, type=Path)
    parser.add_argument("--inference-receipts", required=True, type=Path)
    parser.add_argument("--provider-receipt-index", required=True, type=Path)
    parser.add_argument("--provider-runtime-receipts", required=True, type=Path)
    parser.add_argument("--mint-receipt", required=True, type=Path)
    parser.add_argument("--score-input-output", required=True, type=Path)
    parser.add_argument("--score-receipt-output", required=True, type=Path)
    parser.add_argument("--truth-hmac-key-id", required=True)
    parser.parse_args()
    binding = load_worktree_current_goal_binding(REPOSITORY_ROOT)
    if binding.gate_profile == "CORE_AGENT_GATE":
        parser.error(
            "CORE sealed scoring is reserved for the independent one-shot blind "
            "task after candidate freeze; repository-external signer, broker, "
            "registry and authority activation are not G01-G06 prerequisites"
        )
    if binding.goal_sequence != 7:
        parser.error("HARDENED_CANDIDATE_GATE is restricted to G07")
    parser.error(
        "HARDENED sealed scoring is fail-closed until the repository-external "
        "custodian scorer and signer IPC are activated"
    )


if __name__ == "__main__":
    raise SystemExit(main())
