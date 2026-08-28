from __future__ import annotations

import argparse
from pathlib import Path

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--component", action="append", required=True, type=Path)
    parser.add_argument("--development-checkout", required=True, type=Path)
    parser.add_argument("--candidate-commit", required=True)
    parser.add_argument("--candidate-tree", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.parse_args()
    parser.error(
        "formal final-Gate signing is NOT_RUN during BOOTSTRAP; an activated "
        "repository-external signer IPC must sign without exposing a key path "
        "to the candidate process"
    )


if __name__ == "__main__":
    raise SystemExit(main())
