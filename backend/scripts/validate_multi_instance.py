"""Generate controlled-local evidence for the two-backend topology."""

import asyncio
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import asyncpg
import httpx
import redis.asyncio as redis

from app.api.rate_limit import _redis_allowed


ARTIFACT = Path("evidence/multi_instance/summary.json")


async def _chat(client: httpx.AsyncClient, port: int, thread_id: str, message: str) -> dict:
    response = await client.post(
        f"http://127.0.0.1:{port}/api/chat",
        json={
            "thread_id": thread_id,
            "user_id": "controlled-local-proof",
            "message": message,
            "trip_city": "上海",
            "selected_place_ids": [],
        },
    )
    return {
        "port": port,
        "status": response.status_code,
        "done": '"event": "done"' in response.text,
        "response_bytes": len(response.content),
    }


async def main() -> None:
    thread_id = f"multi-proof-{uuid4().hex}"
    database_url = os.getenv(
        "MULTI_TEST_DATABASE_URL",
        "postgresql://postgres:postgres@127.0.0.1:15432/travel_agent",
    )
    redis_url = os.getenv("MULTI_TEST_REDIS_URL", "redis://127.0.0.1:16379/15")

    async with httpx.AsyncClient(timeout=60) as client:
        health_a, health_b = await asyncio.gather(
            client.get("http://127.0.0.1:18001/health"),
            client.get("http://127.0.0.1:18002/health"),
        )
        health = [health_a.json(), health_b.json()]
        first = await _chat(client, 18001, thread_id, "上海有什么好玩的地方？")

        db = await asyncpg.connect(database_url)
        try:
            after_a = await db.fetchval(
                "SELECT count(*) FROM checkpoints WHERE thread_id = $1",
                thread_id,
            )
            second = await _chat(client, 18002, thread_id, "再推荐一些适合吃饭的地方")
            after_b = await db.fetchval(
                "SELECT count(*) FROM checkpoints WHERE thread_id = $1",
                thread_id,
            )
        finally:
            await db.close()

    redis_client = redis.from_url(redis_url, decode_responses=True)
    rate_key = f"breezetravel:multi-proof:{uuid4().hex}"
    try:
        now = int(time.time() * 1000)
        decisions = await asyncio.gather(
            *[_redis_allowed(redis_client, rate_key, 3, now + index) for index in range(6)]
        )
    finally:
        await redis_client.delete(rate_key)
        await redis_client.aclose()

    instance_ids = sorted(item["instance_id"] for item in health)
    passed = (
        instance_ids == ["backend-a", "backend-b"]
        and first["status"] == second["status"] == 200
        and first["done"]
        and second["done"]
        and after_a > 0
        and after_b > after_a
        and sum(decisions) == 3
    )
    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "scope": "controlled_local_multi_instance",
        "passed": passed,
        "health": health,
        "chat": [first, second],
        "checkpoint_counts": {"after_backend_a": after_a, "after_backend_b": after_b},
        "shared_redis_atomic_limit": {"allowed": sum(decisions), "attempted": len(decisions)},
        "boundaries": [
            "Direct backend ports were used because the pinned nginx image could not be pulled in this run.",
            "Local existing pgvector:pg16 and redis:7-alpine images were used instead of pinned tags.",
            "This is controlled-local evidence, not public deployment evidence.",
        ],
    }
    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not passed:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
