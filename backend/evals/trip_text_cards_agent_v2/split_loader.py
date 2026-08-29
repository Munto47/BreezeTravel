from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from evals.trip_text_cards_v1.contracts import TextCardInputCase


SPLIT_FILES = {
    "dev": (
        "dev.inputs.jsonl",
        54,
        "fb0a55eaae20f4a6ea6bbaee39a7ce6e81c673a43f294bb2953fdc0baf7c6118",
    ),
    "validation": (
        "validation.inputs.jsonl",
        18,
        "1e550a45efe2cfa4efb178c624b677701b3f1edb834a696e39aca9a7226c8780",
    ),
}
TRUTH_KEYS = {
    "answer",
    "annotation",
    "canonical_place",
    "expected",
    "gold",
    "label",
    "oracle",
    "prediction",
    "truth",
}


class AgentSplitValidationError(ValueError):
    pass


@dataclass(frozen=True)
class AgentSplitAccessReceipt:
    split: str
    artifact_path: str
    artifact_sha256: str
    files_opened: int
    blind_inputs_read: int
    blind_truth_read: int


def _contains_truth_key(value: object) -> bool:
    if isinstance(value, dict):
        if any(str(key).casefold() in TRUTH_KEYS for key in value):
            return True
        return any(_contains_truth_key(child) for child in value.values())
    if isinstance(value, list):
        return any(_contains_truth_key(child) for child in value)
    return False


def load_agent_split(
    data_root: Path,
    split: str,
) -> tuple[list[TextCardInputCase], AgentSplitAccessReceipt]:
    if split not in SPLIT_FILES:
        raise AgentSplitValidationError("ordinary agent evaluation cannot open frozen_blind")
    filename, expected_count, expected_sha256 = SPLIT_FILES[split]
    path = data_root / filename
    raw_bytes = path.read_bytes()
    observed_sha256 = hashlib.sha256(raw_bytes).hexdigest()
    if observed_sha256 != expected_sha256:
        raise AgentSplitValidationError(f"{split} input byte binding mismatch")
    values: list[dict[str, object]] = []
    for line_number, line in enumerate(raw_bytes.decode("utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise AgentSplitValidationError(f"{filename}:{line_number} is not an object")
        if _contains_truth_key(value):
            raise AgentSplitValidationError(f"{filename} contains a truth-bearing key")
        values.append(value)
    cases = [TextCardInputCase.model_validate(value) for value in values]
    if len(cases) != expected_count:
        raise AgentSplitValidationError(f"{split} case count mismatch")
    if any(case.split != split for case in cases):
        raise AgentSplitValidationError(f"{split} case has a mismatched split field")
    if len({case.case_id for case in cases}) != len(cases):
        raise AgentSplitValidationError(f"{split} case IDs are not unique")
    return cases, AgentSplitAccessReceipt(
        split=split,
        artifact_path=str(path.resolve()),
        artifact_sha256=observed_sha256,
        files_opened=1,
        blind_inputs_read=0,
        blind_truth_read=0,
    )
