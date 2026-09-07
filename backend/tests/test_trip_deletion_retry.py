from types import SimpleNamespace
from unittest.mock import AsyncMock

import asyncpg
import pytest

from app.trip_understanding.errors import ResourceAccessDeniedError
from app.trip_understanding.models import DeletionOutcome
from app.trip_understanding.service import TripUnderstandingApplicationService


@pytest.mark.asyncio
@pytest.mark.parametrize("failures", [1, 2, 3])
async def test_deadlock_retries_same_authorized_deletion_and_does_not_fake_success(failures):
    delete = AsyncMock(
        side_effect=[asyncpg.DeadlockDetectedError("synthetic") for _ in range(failures)] + [DeletionOutcome()]
    )
    service = TripUnderstandingApplicationService(SimpleNamespace(delete_trip=delete))
    resource = SimpleNamespace(public_resource_id="test-resource", understanding_id="test-trip")
    kwargs = {"capability_hash": "test-capability", "user_id": None, "idempotency_key": "test-delete"}
    if failures == 3:
        with pytest.raises(asyncpg.DeadlockDetectedError):
            await service.delete_trip(resource, **kwargs)
    else:
        assert await service.delete_trip(resource, **kwargs) == DeletionOutcome()
    assert delete.await_count == min(failures + 1, 3)
    assert all(call == delete.await_args_list[0] for call in delete.await_args_list)


@pytest.mark.asyncio
async def test_authorization_failure_is_never_retried():
    delete = AsyncMock(side_effect=ResourceAccessDeniedError("denied"))
    service = TripUnderstandingApplicationService(SimpleNamespace(delete_trip=delete))
    with pytest.raises(ResourceAccessDeniedError):
        await service.delete_trip(
            SimpleNamespace(public_resource_id="test-resource"),
            capability_hash=None,
            user_id="wrong-user",
            idempotency_key="test-delete",
        )
    assert delete.await_count == 1
