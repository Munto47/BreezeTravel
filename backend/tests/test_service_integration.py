import os
import time
from uuid import uuid4

import pytest

from app.api.rate_limit import _redis_allowed


pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_redis_sliding_window_is_shared_and_atomic():
    if os.getenv("RUN_SERVICE_INTEGRATION") != "1":
        pytest.skip("set RUN_SERVICE_INTEGRATION=1 with controlled Redis")
    redis = pytest.importorskip("redis.asyncio")
    client = redis.from_url(os.getenv("TEST_REDIS_URL", "redis://127.0.0.1:6379/15"), decode_responses=True)
    key = f"breezetravel:test:{uuid4().hex}"
    try:
        now = int(time.time() * 1000)
        results = await __import__("asyncio").gather(*[_redis_allowed(client, key, 3, now + index) for index in range(6)])
        assert sum(results) == 3
    finally:
        await client.delete(key)
        await client.aclose()
