"""Account tasks remain findable across page closure, without exposing sources."""
from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from app.trip_understanding.demo import DEMO_SOURCE_TEXT, build_demo_pipeline
from tests.test_experience_readback import application
from tests.test_experience_v3_journey import repository_for


async def create(client, key):
    response = await client.post('/api/v3/trip-understandings', json={
        'mode': 'FULL', 'source': {'type': 'TEXT', 'text': DEMO_SOURCE_TEXT},
    }, headers={'Idempotency-Key': key})
    assert response.status_code == 202, response.text
    return response.json()['public_resource_id']


@pytest.mark.parametrize('kind', ['memory', 'postgres'])
@pytest.mark.asyncio
async def test_new_browser_lists_processing_partial_failed_cancelled_and_recovers_private_source(kind):
    async with repository_for(kind) as repo:
        transport = ASGITransport(app=application(repo))
        headers = {'x-test-user': 'experience-owner'}
        refs = {}
        async with AsyncClient(transport=transport, base_url='http://test', headers=headers) as owner:
            for state in ['FAILED', 'PARTIAL', 'READY', 'CANCELLED', 'PROCESSING']:
                ref = await create(owner, f'account-recover-{state}')
                refs[state] = ref
                now = datetime.now(timezone.utc)
                if state == 'PROCESSING':
                    continue
                if state == 'CANCELLED':
                    stopped = await owner.post(f'/api/v3/trip-understandings/{ref}/cancel',
                        headers={'Idempotency-Key': 'account-recover-stop'})
                    assert stopped.status_code == 200, stopped.text
                    continue
                job = await repo.claim_next(worker_id='account-recovery', now=now, lease_seconds=60)
                assert job is not None
                if state == 'FAILED':
                    await repo.fail_job(job, category='PROVIDER_UNAVAILABLE', now=now, allow_retry=False)
                else:
                    output = await build_demo_pipeline().run(DEMO_SOURCE_TEXT)
                    if state == 'PARTIAL':
                        output.public_result.status = 'PARTIAL_RESULT'
                    await repo.complete_job(job, output, now=now)
        # A fresh client has no anonymous capability or previous in-tab state.
        async with AsyncClient(transport=transport, base_url='http://test', headers=headers) as returning, AsyncClient(
            transport=transport, base_url='http://test') as stranger:
            response = await returning.get('/api/v3/me/trips')
            assert response.status_code == 200 and response.headers['cache-control'] == 'no-store'
            items = {item['state']: item for item in response.json()['items']}
            assert set(items) == set(refs)
            assert DEMO_SOURCE_TEXT not in response.text
            for state, ref in refs.items():
                item = items[state]
                assert item['public_resource_id'] == ref
                assert item['has_result'] == (state in {'READY', 'PARTIAL'})
                assert item['source_status'] == 'AVAILABLE'
                assert not {'source_id', 'understanding_id', 'encrypted_content', 'capability_hash', 'text'} & set(item)
                read = await returning.get(f'/api/v3/trip-understandings/{ref}/result')
                assert read.status_code == (202 if state == 'PROCESSING' else 409 if state in {'FAILED', 'CANCELLED'} else 200)
                source = await returning.get(f'/api/v3/trip-understandings/{ref}/source')
                assert source.status_code == 200 and source.json()['text'] == DEMO_SOURCE_TEXT
                assert (await stranger.get(f'/api/v3/trip-understandings/{ref}/source')).status_code == 404
            assert (await stranger.get('/api/v3/me/trips')).status_code == 401

            failed = refs['FAILED']
            deleted = await returning.delete(f'/api/v3/trip-understandings/{failed}/source',
                headers={'Idempotency-Key': 'delete-failed-source'})
            assert deleted.status_code == 204, deleted.text
            item = next(i for i in (await returning.get('/api/v3/me/trips')).json()['items'] if i['public_resource_id'] == failed)
            assert item['state'] == 'FAILED' and item['source_status'] == 'DELETED'
            assert (await returning.get(f'/api/v3/trip-understandings/{failed}/source')).json()['text'] is None
            assert (await returning.delete(f'/api/v3/trip-understandings/{failed}', headers={
                'Idempotency-Key': 'delete-failed-resource'})).status_code == 204
            assert failed not in {i['public_resource_id'] for i in (await returning.get('/api/v3/me/trips')).json()['items']}
            assert (await returning.get(f'/api/v3/trip-understandings/{failed}/source')).status_code == 410

            expired = datetime.now(timezone.utc) + timedelta(days=31)
            assert not (await repo.list_account_trips(user_id='experience-owner', now=expired)).items


@pytest.mark.parametrize('kind', ['memory', 'postgres'])
@pytest.mark.asyncio
async def test_anonymous_reference_needs_same_live_cookie_and_login_does_not_claim_a_running_job(kind):
    async with repository_for(kind) as repo:
        transport = ASGITransport(app=application(repo))
        async with AsyncClient(transport=transport, base_url='http://test') as browser:
            ref = await create(browser, 'anonymous-recover')
            cookies = browser.cookies
        async with AsyncClient(transport=transport, base_url='http://test', cookies=cookies) as reopened, AsyncClient(
            transport=transport, base_url='http://test', headers={'x-test-user': 'experience-owner'}) as other_browser:
            assert (await reopened.get(f'/api/v3/trip-understandings/{ref}/result')).status_code == 202
            assert (await other_browser.get(f'/api/v3/trip-understandings/{ref}/result')).status_code == 404
            assert not (await other_browser.get('/api/v3/me/trips')).json()['items']
            other_browser.cookies.update(cookies)
            claim = await other_browser.post(f'/api/v3/trip-understandings/{ref}/claim',
                headers={'Idempotency-Key': 'running-is-not-claimed'})
            assert claim.status_code == 409
            assert not (await other_browser.get('/api/v3/me/trips')).json()['items']
            assert (await reopened.get(f'/api/v3/trip-understandings/{ref}/source')).json()['text'] == DEMO_SOURCE_TEXT
