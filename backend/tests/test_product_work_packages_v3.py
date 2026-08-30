from __future__ import annotations

import json
from pathlib import Path

from governance.work_packages_v3 import validate_registry_v3


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def test_g04_registry_proves_frozen_contributions_and_control_bindings() -> None:
    result = validate_registry_v3(REPOSITORY_ROOT)

    assert result["verdict"] == "PASS", result
    assert result["active_goal_id"] == "TC-VNEXT-G04-SCREENSHOT"
    assert result["package_count"] == 4
    assert result["error_codes"] == []


def test_g04_active_slice_excludes_frozen_agent_gate_and_accepts_current_diff() -> None:
    registry = json.loads(
        (
            REPOSITORY_ROOT / "docs/governance/current_work_packages.json"
        ).read_text(encoding="utf-8")
    )
    allowed = registry["active_slice"]["allowed_paths"]
    assert not any(path.startswith("backend/evals/agent_gate_v1/") for path in allowed)

    result = validate_registry_v3(REPOSITORY_ROOT, check_scope=True)
    assert result["verdict"] == "PASS", result
    assert "backend/governance/work_packages_v3.py" in result["changed_paths"]
    assert not any(
        path.startswith("backend/evals/agent_gate_v1/")
        for path in result["changed_paths"]
    )
