from __future__ import annotations

import argparse
from pathlib import Path


def _common_outputs(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--verification-output", required=True, type=Path)
    parser.add_argument("--component-output", required=True, type=Path)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="component", required=True)

    automated = subparsers.add_parser("automated")
    automated.add_argument("--execution-manifest-output", required=True, type=Path)
    automated.add_argument("--runner-image-archive-output", required=True, type=Path)
    _common_outputs(automated)

    live = subparsers.add_parser("live-provider")
    live.add_argument("--amap-provider-index", required=True, type=Path)
    live.add_argument("--amap-runtime", required=True, type=Path)
    live.add_argument("--qwen-runtime", required=True, type=Path)
    _common_outputs(live)

    panel = subparsers.add_parser("panel")
    panel.add_argument("--review", action="append", required=True, type=Path)
    panel.add_argument("--adjudication", required=True, type=Path)
    panel.add_argument("--input-product", required=True, type=Path)
    panel.add_argument("--input-semantic", required=True, type=Path)
    panel.add_argument("--input-reliability", required=True, type=Path)
    _common_outputs(panel)

    sealed = subparsers.add_parser("sealed")
    sealed.add_argument("--receipt", required=True, type=Path)
    sealed.add_argument("--score-input-manifest", required=True, type=Path)
    sealed.add_argument("--deterministic-score-receipt", required=True, type=Path)
    sealed.add_argument("--mint-receipt", required=True, type=Path)
    sealed.add_argument("--thresholds-repository-path", required=True)
    sealed.add_argument("--scorer-repository-path", required=True)
    _common_outputs(sealed)
    return parser


def main() -> int:
    parser = _parser()
    parser.parse_args()
    parser.error(
        "formal component signing is NOT_RUN during BOOTSTRAP; an activated "
        "repository-external signer IPC must sign without exposing a key path "
        "to the candidate process"
    )


if __name__ == "__main__":
    raise SystemExit(main())
