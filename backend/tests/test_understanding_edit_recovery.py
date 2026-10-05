"""Real storage recovery for saved edits and shared inference allowance."""
import asyncio
from datetime import datetime, timezone
import os

import asyncpg
import pytest

from app.trip_understanding.errors import IdempotencyConflictError, RevisionConflictError
from app.trip_understanding.inference_allowance import InferenceAllowanceExceeded
from app.trip_understanding.models import ActivityTextEditCommand
from tests.test_experience_v3_journey import create, repository_for
from tests.test_supplement_jobs import counts, prepared, refresh

ROUNDS = range(int(os.environ.get('TRIPCHECK_RELIABILITY_ROUNDS', '1')))


def note(stored, text):
    return ActivityTextEditCommand(command_type='ACTIVITY_TEXT_EDIT',
        activity_token=stored.result.days[0].activities[0].activity_token, note=text)


@pytest.mark.asyncio
@pytest.mark.parametrize('round_number', ROUNDS)
async def test_two_editors_share_one_base_only_one_can_publish(round_number):
    async with repository_for('postgres') as repo:
        now, resource, original = await prepared(repo)
        before = await counts(repo, resource)
        commands = [note(original, text) for text in ('窗口甲的备注', '窗口乙的备注')]
        outcomes = await asyncio.gather(*(repo.apply_command(resource, command,
            expected_etag=original.opaque_etag, idempotency_key=f'editor-{i}',
            request_hash=str(i) * 64, now=now) for i, command in enumerate(commands)), return_exceptions=True)
        assert sum(isinstance(item, RevisionConflictError) for item in outcomes) == 1
        winner = next(i for i, item in enumerate(outcomes) if not isinstance(item, Exception))
        resource, saved = await refresh(repo, resource)
        assert saved.opaque_etag == outcomes[winner].opaque_etag
        assert saved.result.days[0].activities[0].note == commands[winner].note
        assert (await counts(repo, resource))[0] == before[0] + 1
        assert [command.note for command in commands] == ['窗口甲的备注', '窗口乙的备注']


@pytest.mark.asyncio
@pytest.mark.parametrize('round_number', ROUNDS)
async def test_lost_edit_reply_and_old_replay_never_move_the_current_pointer_back(round_number):
    async with repository_for('postgres') as repo:
        now, resource, original = await prepared(repo)
        command = note(original, '第一次编辑')
        options = dict(expected_etag=original.opaque_etag, idempotency_key='first-edit', request_hash='a'*64, now=now)
        first = await repo.apply_command(resource, command, **options)
        first_counts = await counts(repo, resource)
        replay = await repo.apply_command(resource, command, **options)
        assert replay.replayed and replay.opaque_etag == first.opaque_etag
        assert await counts(repo, resource) == first_counts
        resource, middle = await refresh(repo, resource)
        second = await repo.apply_command(resource, note(middle, '第二次编辑'), expected_etag=middle.opaque_etag,
            idempotency_key='second-edit', request_hash='b'*64, now=now)
        before_replay = await counts(repo, resource)
        replay = await repo.apply_command(resource, command, **options)
        assert replay.replayed and replay.opaque_etag == first.opaque_etag
        resource, current = await refresh(repo, resource)
        assert current.opaque_etag == second.opaque_etag
        assert current.result.days[0].activities[0].note == '第二次编辑'
        assert await counts(repo, resource) == before_replay
        with pytest.raises(IdempotencyConflictError):
            await repo.apply_command(resource, note(original, '不同的编辑'),
                **{**options, 'request_hash':'c'*64})
        assert (await refresh(repo, resource))[1].opaque_etag == current.opaque_etag


@pytest.mark.asyncio
@pytest.mark.parametrize('round_number', ROUNDS)
async def test_edit_operation_write_failure_rolls_back_revision_pointer_and_history(round_number):
    async with repository_for('postgres') as repo:
        now, resource, original = await prepared(repo)
        before = await counts(repo, resource)
        operations = await repo._pool.fetchval('SELECT count(*) FROM trip_understanding_idempotency_records')
        await repo._pool.execute("""CREATE FUNCTION reject_edit_operation() RETURNS trigger LANGUAGE plpgsql AS $$
          BEGIN IF NEW.state='COMPLETED' AND NEW.scope LIKE '%:command' THEN
          RAISE EXCEPTION 'controlled edit failure'; END IF;
          RETURN NEW; END $$;
          CREATE TRIGGER reject_edit_operation BEFORE UPDATE ON trip_understanding_idempotency_records
          FOR EACH ROW EXECUTE FUNCTION reject_edit_operation();""")
        options = dict(expected_etag=original.opaque_etag, idempotency_key='rollback-edit', request_hash='d'*64, now=now)
        with pytest.raises(asyncpg.RaiseError, match='controlled edit failure'):
            await repo.apply_command(resource, note(original, '不应部分保存'), **options)
        resource, current = await refresh(repo, resource)
        assert current.result == original.result and current.opaque_etag == original.opaque_etag
        assert await counts(repo, resource) == before
        assert await repo._pool.fetchval('SELECT count(*) FROM trip_understanding_idempotency_records') == operations
        await repo._pool.execute('DROP TRIGGER reject_edit_operation ON trip_understanding_idempotency_records')
        assert not (await repo.apply_command(resource, note(original, '不应部分保存'), **options)).replayed


@pytest.mark.asyncio
@pytest.mark.parametrize('round_number', ROUNDS)
async def test_parallel_dispatches_cannot_overspend_or_reset_allowance_after_takeover(round_number):
    async with repository_for('postgres') as repo:
        now = datetime.now(timezone.utc)
        await create(repo, 'bounded-dispatch', now)
        original = await repo.claim_next(worker_id='first-worker', now=now, lease_seconds=60)
        await repo._pool.execute('UPDATE trip_understanding_sources SET inference_calls_remaining=1')
        replies = await asyncio.gather(*(repo.reserve_inference_call(original, now=now) for _ in range(2)),
                                       return_exceptions=True)
        assert replies.count(None) == 1
        assert sum(isinstance(item, InferenceAllowanceExceeded) for item in replies) == 1
        source = await repo._pool.fetchrow('SELECT inference_calls_remaining,inference_deadline_at FROM trip_understanding_sources')
        assert source['inference_calls_remaining'] == 0 and source['inference_deadline_at'] is not None
        await repo._pool.execute("UPDATE trip_understanding_jobs SET lease_until=clock_timestamp()-interval '1 second'")
        successor = await repo.claim_next(worker_id='second-worker', now=datetime.now(timezone.utc), lease_seconds=60)
        assert successor.job_id == original.job_id and successor.attempt == original.attempt + 1
        with pytest.raises(InferenceAllowanceExceeded):
            await repo.reserve_inference_call(successor, now=datetime.now(timezone.utc))
        assert await repo._pool.fetchrow('SELECT inference_calls_remaining,inference_deadline_at FROM trip_understanding_sources') == source
