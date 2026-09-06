"""Transfer lockfile-verified npm cache entries when server downloads stall."""
import base64
import hashlib
import json
from pathlib import Path
import subprocess
import tarfile

def main():
    root = Path(__file__).resolve().parents[1]
    cache = Path(subprocess.check_output(['npm.cmd', 'config', 'get', 'cache'], text=True).strip())
    lock = json.loads((root / 'frontend/package-lock.json').read_text(encoding='utf-8'))
    output = root / '.local-artifacts/first-release/npm-cache.tar.gz'
    missing, count = [], 0
    with tarfile.open(output, 'w:gz') as archive:
        seen = set()
        for name, info in lock['packages'].items():
            integrity = info.get('integrity', '')
            if not integrity.startswith('sha512-'):
                continue
            digest = base64.b64decode(integrity.split('-', 1)[1])
            hexed = digest.hex()
            relative = Path('_cacache/content-v2/sha512') / hexed[:2] / hexed[2:4] / hexed[4:]
            source = cache / relative
            if source.is_file():
                assert hashlib.sha512(source.read_bytes()).digest() == digest
                if hexed not in seen:
                    archive.add(source, arcname=relative.as_posix(), recursive=False)
                    count += 1
                    seen.add(hexed)
            elif ('os' not in info or 'linux' in info['os']) and ('cpu' not in info or 'x64' in info['cpu']):
                missing.append(name)
    print(json.dumps({'verified_cache_entries': count, 'uncached_linux_dependencies': missing, 'bytes': output.stat().st_size}))

if __name__ == '__main__':
    main()
