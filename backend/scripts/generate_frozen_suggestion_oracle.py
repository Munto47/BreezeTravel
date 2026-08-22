from __future__ import annotations

from pathlib import Path

from evals.frozen_suggestion_oracle import write_generated_oracle


REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE = REPO_ROOT / "backend/evidence/real_provider_local_authorized/suggestion_snapshot_2026-08-21.json"
DESTINATION = REPO_ROOT / "backend/eval_data/dual_entry_v1/builder_oracles/three_city_frozen_suggestion_ranking_v1.json"


def main() -> None:
    write_generated_oracle(SOURCE, DESTINATION, repo_root=REPO_ROOT)
    print(DESTINATION)


if __name__ == "__main__":
    main()
