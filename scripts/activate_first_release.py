"""Owner-authorized first cutover, retaining both old and new data for recovery."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
from urllib.parse import urlsplit, urlunsplit
from urllib.request import urlopen

ROOT = Path('/opt/breezetravel-releases/first-public-20260906')
BACKUP = Path('/opt/breezetravel-backups/first-release-20260906')
SITE = Path('/etc/nginx/sites-available/breezetravel.cn')
LEGACY_SITE = Path('/etc/nginx/sites-available/travel')
LIVE_DB = 'breeze_live_20260906'
OLD_WRITERS = ['breezetravel-backend-1', 'breezetravel-y-websocket-1']

def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--verified-source-sha', required=True)
    args = parser.parse_args()
    os.umask(0o077)
    assert subprocess.check_output(['hostname'], text=True).strip() == 'iZbp12kpho9obrs2n1564gZ'
    assert not (ROOT / 'activated.json').exists(), 'Already activated; do not repeat cutover'
    assert hashlib.sha256((ROOT / 'source.tar.gz').read_bytes()).hexdigest() == args.verified_source_sha
    for path, digest in json.loads((ROOT / 'source-manifest.json').read_text()).items():
        assert hashlib.sha256((ROOT / 'src' / path).read_bytes()).hexdigest() == digest, path
    original_site = (BACKUP / 'breezetravel.cn').read_bytes()
    original_legacy = LEGACY_SITE.read_bytes()
    assert b'127.0.0.1:3000' in original_legacy and b'listen 8080' in original_legacy
    assert SITE.read_bytes() == original_site, 'Site config changed since backup; inspect before cutover'
    for port, path in [(8018, '/health'), (1246, '/health'), (3118, '/')]:
        assert urlopen(f'http://127.0.0.1:{port}{path}', timeout=5).status == 200
    final = BACKUP / 'cutover'
    final.mkdir(mode=0o700, exist_ok=False)
    shutil.copy2(ROOT / 'private-api.env', final / 'private-staging-api.env')
    (final / 'travel-nginx.conf').write_bytes(original_legacy)
    try:
        # Freeze only BreezeTravel writers long enough to take a final consistent copy.
        run('docker', 'stop', *OLD_WRITERS, 'breeze-first-api', 'breeze-first-yjs', stdout=subprocess.DEVNULL)
        with (final / 'travel_agent.dump').open('xb') as target:
            run('docker', 'exec', 'breezetravel-postgres-1', 'pg_dump', '-U', 'postgres', '-d', 'travel_agent', '-Fc', stdout=target)
        run('docker', 'exec', 'breezetravel-postgres-1', 'createdb', '-U', 'postgres', LIVE_DB)
        with (final / 'travel_agent.dump').open('rb') as source:
            run('docker', 'exec', '-i', 'breezetravel-postgres-1', 'pg_restore', '-U', 'postgres', '-d', LIVE_DB, '--exit-on-error', stdin=source)
        # Never replace or delete the old LevelDB. The staging copy remains recoverable too.
        assert (ROOT / 'yjs-data').resolve().parent == ROOT.resolve()
        (ROOT / 'yjs-data').rename(ROOT / 'staging-yjs-data')
        shutil.copytree('/var/lib/docker/volumes/breezetravel_yjs-data/_data', ROOT / 'yjs-data')
        envpath = ROOT / 'private-api.env'
        values = dict(line.split('=', 1) for line in envpath.read_text().splitlines())
        url = urlsplit(values['DATABASE_URL'])
        values['DATABASE_URL'] = urlunsplit((url.scheme, url.netloc, '/' + LIVE_DB, '', ''))
        envpath.write_text(''.join(f'{k}={v}\n' for k, v in values.items()))
        # These two disposable containers mount the new release; no data volume is removed.
        run('docker', 'rm', 'breeze-first-api', 'breeze-first-yjs', stdout=subprocess.DEVNULL)
        run('python3', str(ROOT / 'run_first_release.py'), 'api', 'yjs', 'web')
        config = original_site.decode().replace('127.0.0.1:8000', '127.0.0.1:8018').replace('127.0.0.1:3000', '127.0.0.1:3118').replace('127.0.0.1:1234', '127.0.0.1:1246')
        config = config.replace('client_max_body_size 60m;', 'client_max_body_size 1m;\n    access_log off;\n    error_log /dev/null;')
        config = config.replace('strict-origin-when-cross-origin', 'no-referrer')
        # Keep opaque resource URLs and websocket tokens out of redirect access logs too.
        config = config.replace('listen 80;', 'listen 80;\n    access_log off;\n    error_log /dev/null;')
        SITE.write_text(config)
        LEGACY_SITE.write_text('server { listen 8080; server_name _; access_log off; error_log /dev/null; return 301 https://www.breezetravel.cn$request_uri; }\n')
        run('nginx', '-t')
        run('systemctl', 'reload', 'nginx')
        marker = {'source_sha256': args.verified_source_sha, 'database': LIVE_DB,
                  'site': 'https://www.breezetravel.cn', 'old_database_retained': True,
                  'status': 'CUTOVER_COMPLETE_PUBLIC_VERIFICATION_PENDING'}
        (ROOT / 'activated.json').write_text(json.dumps(marker, indent=2))
        print('Traffic switched to the verified source; final old data backup and old services retained.')
    except Exception:
        SITE.write_bytes(original_site)
        LEGACY_SITE.write_bytes(original_legacy)
        run('docker', 'start', *OLD_WRITERS, stdout=subprocess.DEVNULL)
        run('nginx', '-t')
        run('systemctl', 'reload', 'nginx')
        print('Cutover did not complete; old site restored. New data retained for investigation.')
        raise

if __name__ == '__main__':
    main()
