from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class CreationOperation(str, Enum):
    CREATE_IMPORT = "CREATE_IMPORT"
    CREATE_AUDIT = "CREATE_AUDIT"
    REFRESH_AUDIT = "REFRESH_AUDIT"
    PROPOSE_REPAIRS = "PROPOSE_REPAIRS"
    GENERATE_TIPS = "GENERATE_TIPS"
    APPLY_TEMPLATE = "APPLY_TEMPLATE"
    PRE_TRIP_RECHECK = "PRE_TRIP_RECHECK"
    REFRESH_CHANGED_ROUTE_EDGES = "REFRESH_CHANGED_ROUTE_EDGES"


@dataclass(frozen=True)
class CreationCommandResponse:
    status_code: int
    body: Any
    headers: dict[str, str]
    idempotent_replay: bool = False

    def as_replay(self) -> "CreationCommandResponse":
        headers = dict(self.headers)
        headers["Idempotency-Replayed"] = "true"
        return CreationCommandResponse(
            status_code=self.status_code,
            body=self.body,
            headers=headers,
            idempotent_replay=True,
        )


@dataclass(frozen=True)
class CreationCommandClaim:
    command_id: str
    workspace_id: str
    operation: CreationOperation
    target_id: str
    actor_user_id: str
    idempotency_key: str
    request_hash: str
    basis: dict[str, Any]
    lease_owner: str | None = None
    replay: CreationCommandResponse | None = None

    @property
    def should_execute(self) -> bool:
        return self.replay is None
