"""Independently recompute and review an external P5 v2 blind bundle."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

from evals.trip_check_v1.p5.blind_custody_v2 import review_blind_label_bundle_v2


def _clean_head(repo_root: Path) -> str:
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()
    dirty = subprocess.run(
        ["git", "status", "--short"],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()
    if dirty:
        raise RuntimeError("review worktree must be clean")
    return head


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--external-bundle", type=Path, required=True)
    parser.add_argument("--external-receipt", type=Path, required=True)
    parser.add_argument("--candidate-subject-commit", required=True)
    args = parser.parse_args()
    head = _clean_head(args.repo_root)
    if head != args.candidate_subject_commit:
        raise RuntimeError("review worktree HEAD differs from candidate subject commit")
    result = review_blind_label_bundle_v2(
        repo_root=args.repo_root,
        external_bundle_path=args.external_bundle,
        external_receipt_path=args.external_receipt,
        candidate_subject_commit=args.candidate_subject_commit,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
