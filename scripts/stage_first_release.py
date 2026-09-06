"""Prepare a side-by-side release on the explicitly authorized existing host.

No traffic switch, service deletion, old database writes, or secret output.
"""
import json
import os
from pathlib import Path
import secrets
import subprocess
from urllib.parse import urlsplit, urlunsplit

RELEASE = Path('/opt/breezetravel-releases/first-public-20260906')
BACKUP = Path('/opt/breezetravel-backups/first-release-20260906')
DB = 'breeze_public_20260906'

def run(*args, **kw):
    return subprocess.run(args, check=True, **kw)

def main():
    assert subprocess.check_output(['hostname'], text=True).strip() == 'iZbp12kpho9obrs2n1564gZ'
    os.umask(0o077)
    assert not (RELEASE / 'activated.json').exists(), 'Do not restage an activated release'
    metadata = json.loads((BACKUP / 'private-container-state.json').read_text())
    old = dict(item.split('=', 1) for item in metadata[0]['Config']['Env'] if '=' in item)
    provider = json.loads((RELEASE / 'private-provider.json').read_text())
    allowed = ('QWEN_API_KEY', 'QWEN_API_URL', 'TRIP_UNDERSTANDING_QWEN_MODEL',
               'TRIP_UNDERSTANDING_QWEN_INPUT_CNY_PER_MILLION', 'TRIP_UNDERSTANDING_QWEN_OUTPUT_CNY_PER_MILLION',
               'AMAP_API_KEY', 'DEEPSEEK_API_KEY', 'DEEPSEEK_API_URL', 'OPENAI_API_KEY', 'OPENAI_API_URL',
               'LLM_MODEL_ROUTER', 'LLM_MODEL_SYNTHESIZER')
    env = {k: provider.get(k) or old.get(k, '') for k in allowed}
    database = urlsplit(old['DATABASE_URL'])
    redis = urlsplit(old.get('REDIS_URL') or 'redis://breezetravel-redis-1:6379')
    # Keep the old account/signing identity; independent v3 keys have never existed in this old DB.
    env.update(DATABASE_URL=urlunsplit((database.scheme, database.netloc.replace('@postgres:', '@breezetravel-postgres-1:'), '/' + DB, '', '')),
               REDIS_URL=urlunsplit((redis.scheme, redis.netloc.replace('redis:', 'breezetravel-redis-1:'), '/8', '', '')),
               JWT_SECRET_KEY=old['JWT_SECRET_KEY'],
               RUNTIME_PROFILE='public', DEMO_MODE='false', AMAP_MOCK='false',
               TRIP_UNDERSTANDING_PROVIDER_MODE='live', EXPERIENCE_WORKERS_ENABLED='true',
               AUTO_MIGRATE='false', REQUIRE_SCHEMA_CHECK='true', CHECKPOINT_BOOTSTRAP_ON_START='false',
               LEGACY_IMPORT_DIAGNOSTICS_ENABLED='false', MEMORY_ENABLED_DEFAULT='false',
               LANGCHAIN_TRACING_V2='false', LANGSMITH_TRACING='false',
               CORS_ORIGIN_REGEX=r'^https://(www\.)?breezetravel\.cn$',
               TRIP_UNDERSTANDING_COOKIE_NAME='bt_capability',
               TRIP_UNDERSTANDING_QWEN_DEADLINE_SECONDS='30',
               TRIP_UNDERSTANDING_QWEN_MAX_OUTPUT_TOKENS='4096')
    envpath = RELEASE / 'private-api.env'
    if envpath.exists():
        existing = dict(line.split('=', 1) for line in envpath.read_text().splitlines() if '=' in line)
    else:
        existing = {}
    for key in ('TRIP_UNDERSTANDING_COOKIE_SIGNING_KEY', 'TRIP_UNDERSTANDING_SOURCE_ENCRYPTION_KEY'):
        env[key] = existing.get(key) or secrets.token_urlsafe(36)
    assert len(env['JWT_SECRET_KEY']) >= 32
    assert env['QWEN_API_KEY'] and env['AMAP_API_KEY'] and env['TRIP_UNDERSTANDING_QWEN_MODEL']
    assert all('\n' not in str(v) for v in env.values())
    envpath.write_text(''.join(f'{k}={v}\n' for k, v in env.items() if v))
    browser = {
        'NEXT_PUBLIC_API_URL': '', 'BACKEND_INTERNAL_URL': 'http://breeze-first-api:8006',
        'NEXT_PUBLIC_Y_WEBSOCKET_URL': 'wss://www.breezetravel.cn/yjs',
        'NEXT_PUBLIC_AMAP_KEY': provider.get('NEXT_PUBLIC_AMAP_KEY') or old.get('AMAP_JS_KEY', ''),
        'NEXT_PUBLIC_AMAP_JS_KEY': provider.get('NEXT_PUBLIC_AMAP_KEY') or old.get('AMAP_JS_KEY', ''),
        'NEXT_PUBLIC_AMAP_SECURITY_CODE': provider.get('NEXT_PUBLIC_AMAP_SECURITY_CODE') or old.get('NEXT_PUBLIC_AMAP_SECURITY_CODE', ''),
    }
    (RELEASE / 'private-web.env').write_text(''.join(f'{k}={json.dumps(v)}\n' for k, v in browser.items()))
    # All of these values are browser-public by design. Server credentials remain outside the build context.
    (RELEASE / 'src/frontend/.env.production').write_text((RELEASE / 'private-web.env').read_text())
    (RELEASE / 'private-yjs.env').write_text(f"JWT_SECRET_KEY={env['JWT_SECRET_KEY']}\nHOST=0.0.0.0\nPORT=1234\nYPERSISTENCE=/data\n")
    exists = subprocess.check_output(['docker', 'exec', 'breezetravel-postgres-1', 'psql', '-U', 'postgres', '-d', 'postgres', '-Atc', f"SELECT 1 FROM pg_database WHERE datname='{DB}'"], text=True).strip()
    if not exists:
        run('docker', 'exec', 'breezetravel-postgres-1', 'createdb', '-U', 'postgres', DB)
        with (BACKUP / 'travel_agent.dump').open('rb') as data:
            run('docker', 'exec', '-i', 'breezetravel-postgres-1', 'pg_restore', '-U', 'postgres', '-d', DB, '--exit-on-error', stdin=data)
    yjs = RELEASE / 'yjs-data'
    if not yjs.exists():
        yjs.mkdir(mode=0o700)
        run('tar', '-C', str(yjs), '-xf', str(BACKUP / 'yjs.tar'))
    print('Private release config, cloned database and copied collaboration data ready; old services untouched.')

if __name__ == '__main__':
    main()
