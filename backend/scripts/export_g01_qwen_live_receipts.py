from __future__ import annotations

import argparse
from pathlib import Path

from evals.agent_gate_v1.authority import load_worktree_current_goal_binding


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence-run-id", required=True)
    parser.add_argument("--candidate-commit", required=True)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.parse_args()
    binding = load_worktree_current_goal_binding(REPOSITORY_ROOT)
    if binding.gate_profile == "CORE_AGENT_GATE":
        parser.error(
            "CORE Qwen export requires completed persisted live inference effects; "
            "repository-external signing, broker and authority activation are not "
            "G01-G06 prerequisites"
        )
    if binding.goal_sequence != 7:
        parser.error("HARDENED_CANDIDATE_GATE is restricted to G07")
    parser.error(
        "HARDENED Qwen export is fail-closed until repository-external capture "
        "and signer IPC are activated"
    )


if __name__ == "__main__":
    raise SystemExit(main())
