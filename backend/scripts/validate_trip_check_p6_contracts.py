"""Validate P6 candidate schemas and optional contract artifacts."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from evals.trip_check_v1.p6.contracts_v1 import (  # noqa: E402
    candidate_gate_eligible,
    load_and_validate,
    validate_candidate_gate_decision,
    validate_final_candidate_evidence,
    validate_schemas,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schemas-only", action="store_true")
    parser.add_argument("--candidate-run-spec", type=Path)
    parser.add_argument("--candidate-evidence", type=Path)
    parser.add_argument("--release-manifest", type=Path)
    parser.add_argument("--candidate-gate-readback", type=Path)
    parser.add_argument("--candidate-gate-receipt", type=Path)
    parser.add_argument("--final-candidate-evidence", type=Path)
    parser.add_argument("--repo-root", type=Path, default=BACKEND_ROOT.parent)
    args = parser.parse_args()

    schema_hashes = validate_schemas()
    artifacts: dict[str, str] = {}
    paths = {
        "candidate_run_spec": args.candidate_run_spec,
        "candidate_evidence": args.candidate_evidence,
        "release_manifest": args.release_manifest,
        "candidate_gate_readback": args.candidate_gate_readback,
        "candidate_gate_receipt": args.candidate_gate_receipt,
    }
    payloads: dict[str, dict[str, object]] = {}
    for kind, path in paths.items():
        if path is not None:
            payload = load_and_validate(path, kind)  # type: ignore[arg-type]
            payloads[kind] = payload
            artifacts[kind] = payload.get("run_spec_hash") or payload.get("manifest_hash") or "VALID"
    if args.final_candidate_evidence is not None:
        final_evidence = load_and_validate(args.final_candidate_evidence, "candidate_evidence")
        payloads["final_candidate_evidence"] = final_evidence
        artifacts["final_candidate_evidence"] = final_evidence["manifest_hash"]

    if not args.schemas_only and not artifacts:
        parser.error("provide --schemas-only or at least one contract artifact")
    status = "SCHEMA_VALID" if not artifacts else "ARTIFACT_VALID"
    eligibility_inputs = {
        "candidate_run_spec", "candidate_evidence", "release_manifest", "candidate_gate_readback",
    }
    final_inputs = eligibility_inputs | {"candidate_gate_receipt", "final_candidate_evidence"}
    if final_inputs.issubset(payloads):
        eligible = candidate_gate_eligible(
            payloads["candidate_evidence"],
            payloads["release_manifest"],
            payloads["candidate_run_spec"],
            payloads["candidate_gate_readback"],
            args.repo_root,
        )
        if eligible:
            validate_candidate_gate_decision(
                payloads["candidate_gate_receipt"],
                payloads["candidate_evidence"],
                payloads["release_manifest"],
                payloads["candidate_gate_readback"],
            )
            validate_final_candidate_evidence(
                payloads["final_candidate_evidence"],
                payloads["candidate_evidence"],
                payloads["release_manifest"],
                payloads["candidate_gate_receipt"],
                payloads["candidate_gate_readback"],
            )
        status = "CANDIDATE_PASS" if eligible else "REJECT"
    elif eligibility_inputs.issubset(payloads) and "candidate_gate_receipt" not in payloads:
        eligible = candidate_gate_eligible(
            payloads["candidate_evidence"],
            payloads["release_manifest"],
            payloads["candidate_run_spec"],
            payloads["candidate_gate_readback"],
            args.repo_root,
        )
        status = "CANDIDATE_ELIGIBLE" if eligible else "REJECT"
    print(json.dumps({"status": status, "schema_hashes": schema_hashes, "artifacts": artifacts}, sort_keys=True))
    if status == "REJECT":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
