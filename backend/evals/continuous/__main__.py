from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from .core import preflight, run_foundation
from .http_builder import run_builder_http
from .http_import import run_import_http


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m evals.continuous")
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate", help="resolve bindings and run fail-closed preflight")
    validate.add_argument("--spec", required=True, type=Path)

    run = subparsers.add_parser("run", help="write a rejected run receipt until product stages exist")
    run.add_argument("--spec", required=True, type=Path)
    run.add_argument("--runs-root", type=Path, default=None, help=argparse.SUPPRESS)

    run_import = subparsers.add_parser("run-import-http", help="execute the controlled-fixture import HTTP slice")
    run_import.add_argument("--spec", required=True, type=Path)
    run_import.add_argument("--runs-root", type=Path, default=None, help=argparse.SUPPRESS)

    run_builder = subparsers.add_parser(
        "run-builder-http", help="execute the public SuggestionSet/accept HTTP slice"
    )
    run_builder.add_argument("--spec", required=True, type=Path)
    run_builder.add_argument("--runs-root", type=Path, default=None, help=argparse.SUPPRESS)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "validate":
        result = preflight(args.spec)
        print(json.dumps(result.summary(), ensure_ascii=False, sort_keys=True))
        return 0 if result.valid else 2

    if args.command == "run-import-http":
        result = run_import_http(args.spec, runs_root=args.runs_root)
    elif args.command == "run-builder-http":
        result = run_builder_http(args.spec, runs_root=args.runs_root)
    else:
        result = run_foundation(args.spec, runs_root=args.runs_root)
    print(json.dumps(result.summary(), ensure_ascii=False, sort_keys=True))
    return 0 if result.gate.get("decision") in {"PROMOTE", "ACCEPT_IMPORT_HTTP_SLICE"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
