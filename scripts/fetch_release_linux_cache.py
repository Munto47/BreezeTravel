"""Fetch exact Linux native packages; verify lockfile integrity before caching."""
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
from pathlib import Path
import tarfile
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]

def fetch(item):
    name, record = item
    url = record['resolved']
    expected = base64.b64decode(record['integrity'].split('-', 1)[1])
    with urlopen(url, timeout=45) as response:
        data = response.read()
    assert hashlib.sha512(data).digest() == expected, name
    h = expected.hex()
    return name, f'_cacache/content-v2/sha512/{h[:2]}/{h[2:4]}/{h[4:]}', data

def main():
    lock = json.loads((ROOT / 'frontend/package-lock.json').read_text(encoding='utf-8'))
    packages = [(name, record) for name, record in lock['packages'].items() if 'linux' in name and 'x64' in name]
    with ThreadPoolExecutor(max_workers=4) as workers, tarfile.open(ROOT / '.local-artifacts/first-release/linux-cache.tar.gz', 'w:gz') as archive:
        for name, path, data in workers.map(fetch, packages):
            entry = tarfile.TarInfo(path)
            entry.size = len(data)
            archive.addfile(entry, io.BytesIO(data))
            print(f'Verified {name}', flush=True)

if __name__ == '__main__':
    main()
