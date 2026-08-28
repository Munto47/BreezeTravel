from __future__ import annotations

import argparse
from pathlib import Path

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence-run-id", required=True)
    parser.add_argument("--candidate-commit", required=True)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.parse_args()
    parser.error(
        "formal Qwen export is NOT_RUN during BOOTSTRAP; the activated "
        "repository-external capture and signer IPC is required"
    )


if __name__ == "__main__":
    raise SystemExit(main())
