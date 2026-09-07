from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = BACKEND_ROOT.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from governance.core_mainline import (  # noqa: E402
    CoreMainlineError,
    product_fingerprint,
    validate_core_mainline,
    validate_delivery_receipt,
)
from governance.experience_delivery import (  # noqa: E402
    ExperienceContractError,
    uses_experience_delivery,
    validate_experience_delivery,
)


def _default_base() -> str:
    github_base = os.environ.get("GITHUB_BASE_REF", "").strip()
    if github_base:
        return f"origin/{github_base}"
    return "origin/develop"


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate the active BreezeTravel product mainline")
    parser.add_argument("--base-ref", default=_default_base())
    parser.add_argument("--head-ref", default="HEAD")
    parser.add_argument("--require-delivery-pass", action="store_true")
    parser.add_argument("--print-product-fingerprint", action="store_true")
    args = parser.parse_args()

    try:
        if uses_experience_delivery(REPOSITORY_ROOT):
            payload = validate_experience_delivery(REPOSITORY_ROOT)
            if args.require_delivery_pass or args.print_product_fingerprint:
                payload["verdict"] = "FAIL"
                payload["errors"].append(
                    "Experience contract checks do not certify product delivery or legacy fingerprints"
                )
            print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
            return 0 if payload["verdict"] == "PASS" else 1
        if args.print_product_fingerprint:
            print(product_fingerprint(REPOSITORY_ROOT))
            return 0
        report = validate_core_mainline(
            REPOSITORY_ROOT,
            base_ref=args.base_ref,
            head_ref=args.head_ref,
        )
        payload = report.model_dump()
        if args.require_delivery_pass:
            payload["delivery_result"] = validate_delivery_receipt(
                REPOSITORY_ROOT,
                report.delivery_goal_sequence,
            )
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if report.verdict == "PASS" else 1
    except (CoreMainlineError, ExperienceContractError) as exc:
        print(json.dumps({"verdict": "FAIL", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
