"""Task-spec lifecycle helpers.

The service keeps parsing deterministic and makes revisions explicit. Database
persistence is optional so unit tests never depend on PostgreSQL import order.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Optional

from app.agents.nodes.task_parser import parse_task_spec
from app.schemas.task_spec import TaskParseResult, TripTaskSpec


class TaskSpecService:
    def parse(
        self,
        text: str,
        *,
        room_id: str,
        default_city: str = "",
        default_days: int = 0,
        current_revision: int = 0,
        memory_preferences: Optional[list[str]] = None,
        start_date: Optional[date] = None,
    ) -> TaskParseResult:
        return parse_task_spec(
            text,
            room_id=room_id,
            default_city=default_city,
            default_days=default_days,
            current_revision=current_revision,
            memory_preferences=memory_preferences,
            start_date=start_date,
        )

    async def save(self, spec: TripTaskSpec, pool) -> TripTaskSpec:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO trip_task_specs(task_id, room_id, task_revision, spec_json, updated_at)
                VALUES ($1, $2, $3, $4::jsonb, NOW())
                ON CONFLICT (task_id) DO UPDATE SET
                    task_revision = EXCLUDED.task_revision,
                    spec_json = EXCLUDED.spec_json,
                    updated_at = NOW()
                """,
                spec.task_id,
                spec.room_id,
                spec.task_revision,
                json.dumps(spec.model_dump(mode="json"), ensure_ascii=False),
            )
        return spec

    async def load_latest(self, room_id: str, pool) -> Optional[TripTaskSpec]:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT spec_json FROM trip_task_specs
                WHERE room_id = $1 ORDER BY task_revision DESC, updated_at DESC LIMIT 1
                """,
                room_id,
            )
        return TripTaskSpec.model_validate(row["spec_json"]) if row else None
