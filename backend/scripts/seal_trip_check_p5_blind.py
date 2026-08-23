"""Seal label-free P5 blind inputs against an external label bundle."""

from __future__ import annotations

import argparse
import json
import re

from evals.trip_check_v1.p5.data_contract import (
    BLIND_INPUT_PATH,
    BLIND_SEAL_PATH,
    MANIFEST_PATH,
    P5_ROOT,
    build_manifest,
    digest,
    file_sha256,
    load_jsonl,
    write_json,
)


_SHA = re.compile(r"^[0-9a-f]{64}$")


def _hash(value: str, name: str) -> str:
    if not _SHA.fullmatch(value):
        raise ValueError(f"{name} must be a lowercase SHA-256")
    return value


def seal(
    *, labels_canonical_sha256: str, external_bundle_sha256: str, review_receipt_sha256: str
) -> dict:
    nonblind = load_jsonl(P5_ROOT / "cases_nonblind_v1.jsonl")
    blind = load_jsonl(BLIND_INPUT_PATH)
    rubric = P5_ROOT / "judge_rubric_v1.json"
    run_spec = P5_ROOT / "run_spec_template_v1.json"
    seal_payload = {
        "schema_version": "trip-check-p5-blind-seal-v1",
        "split": "frozen_blind",
        "case_count": len(blind),
        "case_ids_sha256": digest(sorted(row["case_id"] for row in blind)),
        "inputs_file_sha256": file_sha256(BLIND_INPUT_PATH),
        "inputs_content_sha256": digest(blind),
        "labels_canonical_sha256": _hash(labels_canonical_sha256, "labels_canonical_sha256"),
        "external_bundle_sha256": _hash(external_bundle_sha256, "external_bundle_sha256"),
        "rubric_sha256": file_sha256(rubric),
        "run_spec_template_sha256": file_sha256(run_spec),
        "variant_ids_sha256": digest(["legacy_a", "core_b", "solver_c"]),
        "review_receipt_sha256": _hash(review_receipt_sha256, "review_receipt_sha256"),
        "label_storage": "external_bundle_only",
        "label_access": "isolated_scorer_only",
        "scoring_payload_present": False,
        "human_evidence": False,
    }
    write_json(BLIND_SEAL_PATH, seal_payload)
    manifest = build_manifest(nonblind, blind, seal_payload)
    write_json(MANIFEST_PATH, manifest)
    return {"seal": seal_payload, "manifest": manifest}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--labels-canonical-sha256", required=True)
    parser.add_argument("--external-bundle-sha256", required=True)
    parser.add_argument("--review-receipt-sha256", required=True)
    args = parser.parse_args()
    result = seal(
        labels_canonical_sha256=args.labels_canonical_sha256,
        external_bundle_sha256=args.external_bundle_sha256,
        review_receipt_sha256=args.review_receipt_sha256,
    )
    print(json.dumps({
        "schema_version": result["seal"]["schema_version"],
        "case_count": result["seal"]["case_count"],
        "manifest_hash": result["manifest"]["manifest_hash"],
    }, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
