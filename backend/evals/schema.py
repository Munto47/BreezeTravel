from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, model_validator


class EvalSplit(str, Enum):
    PILOT = "pilot"
    DEV = "dev"
    BLIND = "blind"


class EvalKind(str, Enum):
    ROUTER = "router"
    RAG_CLAIM = "rag_claim"
    TASK_PARSE = "task_parse"
    VERIFIER = "verifier"
    END_TO_END = "end_to_end"
    FAULT = "fault"


class EvalCase(BaseModel):
    id: str
    kind: EvalKind
    split: EvalSplit
    city: str
    input: dict[str, Any]
    expected: dict[str, Any]
    source_snapshot: str
    review_status: Literal["programmatically_reviewed", "single_reviewed", "double_checked", "pending"]
    provenance: str
    case_hash: str
    fault_profile: Optional[str] = None
    tags: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def blind_must_not_be_pending(self):
        if self.split == EvalSplit.BLIND and self.review_status == "pending":
            raise ValueError("blind cases cannot be pending review")
        return self


class EvaluationRun(BaseModel):
    run_id: str
    kind: EvalKind
    split: EvalSplit
    commit_sha: str
    dataset_hash: str
    corpus_hash: Optional[str] = None
    config_hash: str
    model: Optional[str] = None
    prompt_version: Optional[str] = None
    environment: str
    seed: int
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    metrics: dict[str, Any] = Field(default_factory=dict)
    buckets: dict[str, Any] = Field(default_factory=dict)
    raw_results: list[dict[str, Any]] = Field(default_factory=list)
    bad_cases: list[dict[str, Any]] = Field(default_factory=list)
    production_claim_not_made: bool = True
