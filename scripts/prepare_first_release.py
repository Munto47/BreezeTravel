"""Owner-authorized, read-only source backup before the first server release.

Run on the known host. Never removes data, changes services, or prints secrets.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

BACKUP = Path('/opt/breezetravel-backups/first-release-20260906')


def main():
    assert subprocess.check_output(['hostname'], text=True).strip() == 'iZbp12kpho9obrs2n1564gZ'
    os.umask(0o077)
    BACKUP.mkdir(mode=0o700, exist_ok=False)
    names = ['breezetravel-backend-1', 'breezetravel-frontend-1',
             'breezetravel-postgres-1', 'breezetravel-redis-1', 'breezetravel-y-websocket-1']
    metadata = subprocess.check_output(['docker', 'inspect', *names])
    (BACKUP / 'private-container-state.json').write_bytes(metadata)
    source = Path('/opt/breezetravel-releases/d282035aef338bb622dde55be2585f624fc77190/src')
    shutil.copy2(source / '.env', BACKUP / 'private-old.env')
    shutil.copy2(source / 'deploy/p6/docker-compose.candidate.yml', BACKUP / 'old-compose.yml')
    shutil.copy2('/etc/nginx/sites-available/breezetravel.cn', BACKUP / 'breezetravel.cn')
    with (BACKUP / 'travel_agent.dump').open('xb') as output:
        subprocess.run(['docker', 'exec', 'breezetravel-postgres-1', 'pg_dump', '-U', 'postgres', '-d', 'travel_agent', '-Fc'], stdout=output, check=True)
    with (BACKUP / 'yjs.tar').open('xb') as output:
        subprocess.run(['tar', '-C', '/var/lib/docker/volumes/breezetravel_yjs-data/_data', '-cf', '-', '.'], stdout=output, check=True)
    # Read the archive table of contents without disclosing business data.
    with (BACKUP / 'travel_agent.dump').open('rb') as data:
        result = subprocess.run(['docker', 'exec', '-i', 'breezetravel-postgres-1', 'pg_restore', '-l'], stdin=data, stdout=subprocess.DEVNULL)
        result.check_returncode()
    entries = {p.name: {'bytes': p.stat().st_size, 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()}
               for p in BACKUP.iterdir() if p.is_file()}
    (BACKUP / 'manifest.json').write_text(json.dumps(entries, indent=2))
    print('Old database, Yjs, configuration and container identities backed up; archive readback passed.')


if __name__ == '__main__':
    main()
