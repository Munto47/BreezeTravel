"""Build P5 executable non-blind cases and label-free blind inputs."""

from __future__ import annotations

import argparse
import json

from evals.trip_check_v1.p5.data_contract import (
    BLIND_INPUT_PATH,
    NONBLIND_PATH,
    build_blind_inputs,
    build_nonblind_cases,
    split_summary,
    write_jsonl,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    nonblind = build_nonblind_cases()
    blind = build_blind_inputs()
    if args.write:
        write_jsonl(NONBLIND_PATH, nonblind)
        write_jsonl(BLIND_INPUT_PATH, blind)
    print(json.dumps({
        "schema_version": "trip-check-p5-build-result-v1",
        "write": args.write,
        "nonblind": split_summary(nonblind),
        "frozen_blind": split_summary(blind),
    }, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
