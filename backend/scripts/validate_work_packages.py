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
    validate_ready_to_merge_package,
)
from evals.agent_gate_v1.scope_guard import validate_mainline_scope  # noqa: E402
from governance.work_packages_v3 import validate_registry_v3  # noqa: E402
from governance.experience_delivery import (  # noqa: E402
    ExperienceContractError,
    uses_experience_delivery,
    validate_experience_delivery,
)


REPOSITORY_ROOT = BACKEND_ROOT.parent


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--package-id")
    parser.add_argument("--ready-package-id")
    parser.add_argument("--scope-check", action="store_true")
    parser.add_argument(
        "--phase",
        choices=(
            "IMPLEMENTING",
            "REPAIR_ACTIVE",
            "PREFLIGHT",
            "DELIVERY_VERIFY",
            "GOAL_TRANSITION",
            "EVIDENCE_FROZEN",
            "GATE_RUNNING",
            "DELIVERY_PASS_RECORDED",
            "TRANSITION_READY",
        ),
    )
    parser.add_argument("--audit-worktree", type=Path)
    parser.add_argument("--evidence-receipt", action="append", type=Path, default=[])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    modes = sum(bool(value) for value in (args.package_id, args.ready_package_id, args.scope_check))
    if modes > 1:
        parser.error("choose only one package validation mode")
    if not args.scope_check and any(
        value for value in (args.phase, args.audit_worktree, args.evidence_receipt, args.output)
    ):
        parser.error("scope options require --scope-check")
    target_root = (args.audit_worktree or REPOSITORY_ROOT).resolve()
    try:
        is_experience = uses_experience_delivery(target_root)
    except ExperienceContractError as exc:
        print(json.dumps({"verdict": "FAIL", "errors": [str(exc)]}, ensure_ascii=False))
        return 2
    if is_experience:
        result = validate_experience_delivery(target_root)
        if any((args.package_id, args.ready_package_id, args.phase, args.evidence_receipt, args.output)):
            result["verdict"] = "FAIL"
            result["errors"].append("Legacy package promotion and receipt options do not apply to the experience contract")
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if result["verdict"] == "PASS" else 2
    registry_raw = json.loads(
        (REPOSITORY_ROOT / "docs/governance/current_work_packages.json").read_text(
            encoding="utf-8"
        )
    )
    is_v3 = registry_raw.get("schema_version") == "work-package-registry-v3"
    if is_v3:
        target_root = (args.audit_worktree or REPOSITORY_ROOT).resolve()
        result = validate_registry_v3(
            target_root,
            check_scope=args.scope_check,
            package_id=args.package_id or args.ready_package_id,
        )
        if args.phase is not None:
            target_registry = json.loads(
                (
                    target_root / "docs/governance/current_work_packages.json"
                ).read_text(encoding="utf-8")
            )
            if target_registry.get("active_slice", {}).get("phase") != args.phase:
                result["error_codes"].append("REQUESTED_PHASE_MISMATCH")
                result["error_codes"] = sorted(set(result["error_codes"]))
                result["verdict"] = "FAIL"
        rendered = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        if args.output is not None:
            output = args.output.resolve()
            repository = REPOSITORY_ROOT.resolve()
            if output == repository or repository in output.parents:
                parser.error("scope report output must stay outside the repository")
            output.parent.mkdir(parents=True, exist_ok=True)
            with output.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(rendered)
        print(rendered, end="")
        return 0 if result["verdict"] == "PASS" else 2
    if args.scope_check:
        result = validate_mainline_scope(
            REPOSITORY_ROOT,
            target_root=args.audit_worktree,
            requested_phase=args.phase,
            evidence_receipts=args.evidence_receipt,
        )
        payload = result.model_dump(mode="json")
        rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        if args.output is not None:
            output = args.output.resolve()
            repository = REPOSITORY_ROOT.resolve()
            if output == repository or repository in output.parents:
                parser.error("scope report output must stay outside the repository")
            output.parent.mkdir(parents=True, exist_ok=True)
            with output.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(rendered)
        print(rendered, end="")
        return 0 if result.verdict == "PASS" else 2
    if args.package_id or args.ready_package_id:
        scope = validate_mainline_scope(REPOSITORY_ROOT)
        if scope.verdict != "PASS":
            parser.error(
                "scope guard rejected work-package transition: "
                f"{scope.verdict} {scope.error_codes}"
            )
    if args.ready_package_id:
        result = validate_ready_to_merge_package(
            REPOSITORY_ROOT,
            args.ready_package_id,
        )
        payload = result.model_dump(mode="json")
    elif args.package_id:
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
