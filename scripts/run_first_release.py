"""Start only this new, named release on private ports; never switch traffic."""
import argparse
import json
from pathlib import Path
import subprocess
import time
from urllib.request import urlopen

ROOT = Path('/opt/breezetravel-releases/first-public-20260906')
IMAGES = {
    'api': 'sha256:95cda8807e2731aa0aee468a145f0eb05f8b965f9c344910cf05f3ae6f30866c',
    'web': 'sha256:9ebd892ab98e86d802b96c80e163db69dda1a3693cae2dfe2c1a15a3306ee2c2',
    'yjs': 'sha256:485583883b82a268cbc27c2093efbcf293d62bb04dccedaa63c740a6c4012a3a',
}

def start(kind):
    name = f'breeze-first-{kind}'
    if kind == 'web':
        for relative in ('public', '.next/static'):
            (ROOT / 'src/frontend/.next/standalone' / relative).mkdir(parents=True, exist_ok=True)
    existing = subprocess.run(['docker', 'inspect', name], capture_output=True)
    if existing.returncode == 0:
        state = json.loads(existing.stdout)[0]
        assert state['Config']['Labels'].get('breeze.release') == 'first-public-20260906'
        if not state['State']['Running']:
            subprocess.run(['docker', 'start', name], check=True, stdout=subprocess.DEVNULL)
        return
    args = ['docker', 'run', '-d', '--name', name, '--restart', 'unless-stopped',
            '--label', 'breeze.release=first-public-20260906', '--network', 'breezetravel_default',
            '--log-opt', 'max-size=10m', '--log-opt', 'max-file=3']
    if kind == 'api':
        args += ['--memory=900m', '-p', '127.0.0.1:8018:8006', '--env-file', str(ROOT / 'private-api.env'),
                 '-v', f'{ROOT}/src/backend:/release/backend:ro',
                 '-v', f'{ROOT}/src/scripts:/release/scripts:ro', '-w', '/release',
                 '--entrypoint', 'python', IMAGES[kind], 'scripts/experience_container.py']
    elif kind == 'yjs':
        args += ['--memory=200m', '-p', '127.0.0.1:1246:1234', '--env-file', str(ROOT / 'private-yjs.env'),
                 '-v', f'{ROOT}/src/y-websocket/server.js:/app/server.js:ro',
                 '-v', f'{ROOT}/yjs-data:/data', '-w', '/app', '--entrypoint', 'node', IMAGES[kind], 'server.js']
    else:
        assert (ROOT / 'src/frontend/.next/standalone/server.js').is_file()
        args += ['--memory=400m', '-p', '127.0.0.1:3118:3000', '-e', 'NODE_ENV=production',
                 '-e', 'HOSTNAME=0.0.0.0', '-e', 'PORT=3000', '-e', 'NEXT_TELEMETRY_DISABLED=1',
                 '-v', f'{ROOT}/src/frontend/.next/standalone:/release:ro',
                 '-v', f'{ROOT}/src/frontend/.next/static:/release/.next/static:ro',
                 '-v', f'{ROOT}/src/frontend/public:/release/public:ro',
                 '-w', '/release', '--entrypoint', 'node', IMAGES[kind], 'server.js']
    subprocess.run(args, check=True, stdout=subprocess.DEVNULL)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('services', nargs='+', choices=['api', 'web', 'yjs'])
    args = parser.parse_args()
    for service in args.services:
        start(service)
    ports = {'api': (8018, '/health'), 'web': (3118, '/'), 'yjs': (1246, '/health')}
    for service in args.services:
        port, path = ports[service]
        for _ in range(45):
            try:
                with urlopen(f'http://127.0.0.1:{port}{path}', timeout=2) as response:
                    assert response.status == 200
                    if service == 'api':
                        body = json.load(response)
                        assert body == {'status': 'ok'}
                        subprocess.run(['docker', 'exec', '-w', '/release/backend', 'breeze-first-api', 'python', '-c',
                                        "from app.config import settings; assert settings.runtime_profile == 'public' and not settings.amap_mock and settings.trip_understanding_provider_mode == 'live'"], check=True, stdout=subprocess.DEVNULL)
                print(f'{service}: private health check passed')
                break
            except Exception:
                time.sleep(1)
        else:
            raise SystemExit(f'{service}: startup check failed; old site unchanged')

if __name__ == '__main__':
    main()
