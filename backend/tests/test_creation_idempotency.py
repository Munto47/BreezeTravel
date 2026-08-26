from __future__ import annotations

import pytest

from app.itineraries.errors import IdempotencyKeyReusedError
from app.itineraries.hash_service import sha256_canonical
from app.operations.errors import IdempotencyLeaseLostError, IdempotencyRequestInProgressError
from app.operations.models import CreationCommandResponse, CreationOperation
from app.operations.repositories import InMemoryCreationCommandRepository


def _request_hash(body: dict) -> str:
    return sha256_canonical(
        {
            "schema_version": 1,
            "operation": CreationOperation.CREATE_IMPORT.value,
            "workspace_id": "workspace-1",
            "target_id": "workspace-1",
            "actor_user_id": "user-1",
            "body": body,
        }
    )


@pytest.mark.asyncio
async def test_same_key_replays_stored_response_and_different_payload_is_rejected():
    repository = InMemoryCreationCommandRepository()
    request_hash = _request_hash({"raw_text": "第1天：故宫"})
    claim = await repository.claim(
        workspace_id="workspace-1",
        operation=CreationOperation.CREATE_IMPORT,
        target_id="workspace-1",
        actor_user_id="user-1",
        idempotency_key="create-1",
        request_hash=request_hash,
        basis={"current_import_id": None},
    )

    with pytest.raises(IdempotencyRequestInProgressError) as in_progress:
        await repository.claim(
            workspace_id="workspace-1",
            operation=CreationOperation.CREATE_IMPORT,
            target_id="workspace-1",
            actor_user_id="user-1",
            idempotency_key="create-1",
            request_hash=request_hash,
            basis={"current_import_id": None},
        )
    assert in_progress.value.code == "IDEMPOTENCY_REQUEST_IN_PROGRESS"

    calls = 0

    async def finalize(_conn, basis):
        nonlocal calls
        calls += 1
        assert basis == {"current_import_id": None}
        return CreationCommandResponse(
            status_code=201,
            body={"import_id": "import-1"},
            headers={"ETag": '"2"'},
        )

    first = await repository.finalize(claim, finalize)
    replay_claim = await repository.claim(
        workspace_id="workspace-1",
        operation=CreationOperation.CREATE_IMPORT,
        target_id="workspace-1",
        actor_user_id="user-1",
        idempotency_key="create-1",
        request_hash=request_hash,
        basis={"current_import_id": "changed-after-commit"},
    )
    assert first.body == replay_claim.replay.body
    assert replay_claim.replay.headers == {"ETag": '"2"', "Idempotency-Replayed": "true"}
    assert calls == 1

    with pytest.raises(IdempotencyKeyReusedError) as reused:
        await repository.claim(
            workspace_id="workspace-1",
            operation=CreationOperation.CREATE_IMPORT,
            target_id="workspace-1",
            actor_user_id="user-1",
            idempotency_key="create-1",
            request_hash=_request_hash({"raw_text": "第1天：天坛"}),
            basis={"current_import_id": None},
        )
    assert reused.value.code == "IDEMPOTENCY_KEY_REUSED"


@pytest.mark.asyncio
async def test_abandoned_claim_can_be_taken_over_but_key_remains_bound_to_hash():
    repository = InMemoryCreationCommandRepository()
    request_hash = _request_hash({"raw_text": "第1天：故宫"})
    claim = await repository.claim(
        workspace_id="workspace-1",
        operation=CreationOperation.CREATE_IMPORT,
        target_id="workspace-1",
        actor_user_id="user-1",
        idempotency_key="create-abandoned",
        request_hash=request_hash,
        basis={"current_import_id": None},
    )
    await repository.abandon(claim)
    replacement = await repository.claim(
        workspace_id="workspace-1",
        operation=CreationOperation.CREATE_IMPORT,
        target_id="workspace-1",
        actor_user_id="user-1",
        idempotency_key="create-abandoned",
        request_hash=request_hash,
        basis={"current_import_id": "must-not-replace-stored-basis"},
    )
    assert replacement.command_id == claim.command_id
    assert replacement.basis == {"current_import_id": None}

    async def stale_finalize(_conn, _basis):
        return CreationCommandResponse(status_code=200, body={"stale": True}, headers={})

    with pytest.raises(IdempotencyLeaseLostError):
        await repository.finalize(claim, stale_finalize)

    await repository.abandon(replacement)
    with pytest.raises(IdempotencyKeyReusedError):
        await repository.claim(
            workspace_id="workspace-1",
            operation=CreationOperation.CREATE_IMPORT,
            target_id="workspace-1",
            actor_user_id="user-1",
            idempotency_key="create-abandoned",
            request_hash=_request_hash({"raw_text": "异 payload"}),
            basis={"current_import_id": None},
        )
