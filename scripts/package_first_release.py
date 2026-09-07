"""Package allowlisted tracked runtime source, not local data or credentials."""
import hashlib
import json
from pathlib import Path
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / '.local-artifacts' / 'first-release'

def main():
    OUT.mkdir(parents=True, exist_ok=True)
    tracked = subprocess.check_output(['git', 'ls-files', '-z'], cwd=ROOT).decode().split('\0')
    exact = {'frontend/package.json', 'frontend/package-lock.json', 'frontend/next.config.ts',
             'frontend/tsconfig.json', 'frontend/tailwind.config.ts',
             'frontend/postcss.config.js', 'scripts/experience.py', 'scripts/experience_container.py',
             'y-websocket/server.js',
             'y-websocket/package.json', 'y-websocket/package-lock.json'}
    names = sorted(set(exact) | {n for n in tracked if n.startswith(('backend/app/', 'frontend/src/', 'frontend/public/'))})
    manifest = {}
    with tarfile.open(OUT / 'source.tar.gz', 'w:gz') as archive:
        for name in names:
            source = ROOT / name
            assert source.is_file() and not source.is_symlink()
            assert not any(p in {'.env', 'node_modules', '__pycache__'} for p in source.parts)
            manifest[name] = hashlib.sha256(source.read_bytes()).hexdigest()
            archive.add(source, arcname=name, recursive=False)
    (OUT / 'source-manifest.json').write_text(json.dumps(manifest, indent=2))
    print(f'Packaged {len(names)} runtime files; archive SHA256 {hashlib.sha256((OUT / "source.tar.gz").read_bytes()).hexdigest()}')

if __name__ == '__main__':
    main()
