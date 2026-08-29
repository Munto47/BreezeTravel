from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from evals.agent_gate_v1.work_packages import (  # noqa: E402
    load_work_package_registry,
    validate_package_checkout,
)


REPOSITORY_ROOT = BACKEND_ROOT.parent


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--package-id")
    args = parser.parse_args()
    if args.package_id:
        result = validate_package_checkout(REPOSITORY_ROOT, args.package_id)
        payload = result.model_dump(mode="json")
    else:
        result = load_work_package_registry(REPOSITORY_ROOT)
        payload = {
            "active_goal_id": result.active_goal_id,
            "mainline_phase": result.mainline_phase,
            "gate_profile": result.gate_profile,
            "package_count": len(result.packages),
            "verdict": "PASS",
        }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
