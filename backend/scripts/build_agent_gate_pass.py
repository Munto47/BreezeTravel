from __future__ import annotations

import argparse
from pathlib import Path

from evals.agent_gate_v1.authority import load_worktree_current_goal_binding


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--component", action="append", required=True, type=Path)
    parser.add_argument("--development-checkout", required=True, type=Path)
    parser.add_argument("--candidate-commit", required=True)
    parser.add_argument("--candidate-tree", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.parse_args()
    binding = load_worktree_current_goal_binding(REPOSITORY_ROOT)
    if binding.gate_profile == "CORE_AGENT_GATE":
        parser.error(
            "CORE_AGENT_GATE uses the unsigned deterministic clean-checkout "
            "aggregator; HARDENED final signing is not part of G01-G06"
        )
    if binding.goal_sequence != 7:
        parser.error("HARDENED_CANDIDATE_GATE is restricted to G07")
    parser.error(
        "HARDENED final-Gate signing is fail-closed until an activated "
        "repository-external signer IPC must sign without exposing a key path "
        "to the candidate process"
    )


if __name__ == "__main__":
    raise SystemExit(main())
