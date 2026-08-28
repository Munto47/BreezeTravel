from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from evals.agent_gate_v1.path_security import (
    discover_repository_root,
    require_canonical_data_root,
    write_external_bytes_exclusive,
)
from evals.trip_text_cards_agent_v2.annotations import (
    build_blank_agent_work_packet,
    validate_provider_receipt_assets,
)
from evals.trip_text_cards_agent_v2.split_loader import load_agent_split


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--split", required=True, choices=("dev", "validation"))
    parser.add_argument("--assignment-id", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--provider-receipt-index", required=True, type=Path)
    parser.add_argument("--provider-runtime-receipts", required=True, type=Path)
    parser.add_argument("--expected-candidate-commit", required=True)
    parser.add_argument("--expected-candidate-tree", required=True)
    parser.add_argument("--expected-provider-binding-sha256", required=True)
    parser.add_argument("--expected-runtime-bundle-sha256", required=True)
    parser.add_argument("--expected-database-export-receipt-sha256", required=True)
    parser.add_argument("--expected-provider-http-receipt-bundle-sha256", required=True)
    parser.add_argument("--data-root", type=Path, default=Path("eval_data/trip_text_cards_v1"))
    parser.add_argument(
        "--prompt",
        type=Path,
        default=Path("eval_data/trip_text_cards_agent_v2/prompts/reference.md"),
    )
    args = parser.parse_args()

    repository_root = discover_repository_root(Path(__file__).parent)
    data_root = require_canonical_data_root(args.data_root, repository_root)
    prompt = args.prompt.resolve(strict=True)
    source_cases, access_receipt = load_agent_split(data_root, args.split)
    provider_index, _runtime_bundle, provider_verification = validate_provider_receipt_assets(
        split=args.split,
        provider_receipt_index_path=args.provider_receipt_index,
        provider_runtime_receipt_bundle_path=args.provider_runtime_receipts,
        repository_root=repository_root,
        expected_candidate_commit=args.expected_candidate_commit,
        expected_candidate_tree=args.expected_candidate_tree,
        expected_goal_id="TC-VNEXT-G01-TEXT-CARDS",
        expected_provider_binding_sha256=args.expected_provider_binding_sha256,
        expected_runtime_receipt_bundle_sha256=args.expected_runtime_bundle_sha256,
        expected_database_export_receipt_sha256=(
            args.expected_database_export_receipt_sha256
        ),
        expected_provider_http_receipt_bundle_sha256=(
            args.expected_provider_http_receipt_bundle_sha256
        ),
        require_live_provider_evidence=True,
    )
    packet = build_blank_agent_work_packet(
        split=args.split,
        assignment_id=args.assignment_id,
        source_cases=source_cases,
        prompt_sha256=hashlib.sha256(prompt.read_bytes()).hexdigest(),
        provider_receipt_index=provider_index,
        provider_receipt_index_sha256=provider_verification[
            "provider_receipt_index_sha256"
        ],
    )
    output_bytes = (
        json.dumps(packet, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    output = write_external_bytes_exclusive(
        args.output,
        output_bytes,
        repository_root,
    ).path
    print(
        json.dumps(
            {
                "output": str(output),
                "case_count": len(packet["cases"]),
                "labels": 0,
                "provider_receipt_index_sha256": provider_verification[
                    "provider_receipt_index_sha256"
                ],
                "provider_runtime_receipt_bundle_sha256": provider_verification[
                    "provider_runtime_receipt_bundle_sha256"
                ],
                "input_access": access_receipt.__dict__,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
