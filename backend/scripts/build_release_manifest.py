"""Build a reproducible, secret-free release evidence manifest.

The script is intentionally honest about a dirty working tree: it records the
commit plus a diff hash and never labels uncommitted code as a clean release.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path


BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent


def sha256_file(path: Path) -> str | None:
    if not path.exists():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=ROOT, text=True, encoding="utf-8",
        errors="replace", capture_output=True, check=True,
    )
    return result.stdout.strip()


def config_summary() -> dict[str, object]:
    # Values are policy switches only. Credentials, URLs carrying credentials,
    # user identifiers and prompts are deliberately excluded.
    return {
        "runtime_profile": os.getenv("RUNTIME_PROFILE", "local_real"),
        "demo_mode": os.getenv("DEMO_MODE", "false").lower() == "true",
        "amap_mock": os.getenv("AMAP_MOCK", "true").lower() == "true",
        "ft_router_enabled": os.getenv("FT_ROUTER_ENABLED", "false").lower() == "true",
        "reranker_enabled": os.getenv("RERANKER_ENABLED", "false").lower() == "true",
        "auto_migrate": os.getenv("AUTO_MIGRATE", "false").lower() == "true",
        "required_migration": os.getenv("REQUIRED_MIGRATION", "008_task_security_memory.sql"),
    }


def working_tree_fingerprint() -> tuple[str, int]:
    """Hash tracked changes plus untracked files without self-hashing releases."""
    digest = hashlib.sha256()
    digest.update(git("diff", "--binary", "HEAD").encode())
    result = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
        capture_output=True,
        check=True,
    )
    paths = sorted(item.decode("utf-8", errors="surrogateescape") for item in result.stdout.split(b"\0") if item)
    included = 0
    for relative in paths:
        normalised = relative.replace("\\", "/")
        if normalised.startswith("backend/evidence/releases/"):
            continue
        path = ROOT / relative
        if not path.is_file():
            continue
        digest.update(normalised.encode("utf-8", errors="surrogateescape"))
        digest.update(b"\0")
        digest.update((sha256_file(path) or "").encode())
        included += 1
    return digest.hexdigest(), included


def build(output_root: Path) -> Path:
    commit = git("rev-parse", "HEAD")
    status = git("status", "--porcelain=v1")
    dirty = bool(status)
    tree_hash, untracked_count = working_tree_fingerprint() if dirty else ("", 0)
    release_id = f"{commit[:12]}-dirty-{tree_hash[:12]}" if dirty else commit
    migrations = sorted((BACKEND / "app" / "db" / "migrations").glob("*.sql"))
    eval_manifest = BACKEND / "eval_data" / "manifest.json"
    payload = {
        "schema_version": "1.0",
        "release_id": release_id,
        "commit_sha": commit,
        "working_tree_clean": not dirty,
        "working_tree_diff_sha256": tree_hash if dirty else None,
        "untracked_files_hashed": untracked_count,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "toolchains": {"python": "3.11", "node": "20", "postgres_pgvector": "0.8.1-pg16", "redis": "7.4.2"},
        "dependency_locks": {
            "backend_requirements": sha256_file(BACKEND / "requirements.txt"),
            "backend_dev_requirements": sha256_file(BACKEND / "requirements-dev.txt"),
            "frontend_package_lock": sha256_file(ROOT / "frontend" / "package-lock.json"),
            "yjs_package_lock": sha256_file(ROOT / "y-websocket" / "package-lock.json"),
        },
        "migrations": [{"name": item.name, "sha256": sha256_file(item)} for item in migrations],
        "latest_migration": migrations[-1].name if migrations else None,
        "configuration": config_summary(),
        "evaluation_manifest_sha256": sha256_file(eval_manifest),
        "evidence_paths": {
            "local_eval": "backend/evidence/local_eval/summary.json",
            "fault_injection": "backend/evidence/fault_injection/summary.json",
            "experiments": "backend/evidence/experiments/summary.json",
            "multi_instance": "backend/evidence/multi_instance/summary.json",
        },
        "verification_commands": [
            "powershell -ExecutionPolicy Bypass -File .\\verify-local.ps1",
            "docker compose config --quiet",
            "docker compose -f docker-compose.multi.yml config --quiet",
        ],
        "excluded_claims": ["public deployment", "public smoke", "real-user validation", "production SLO"],
    }
    target = output_root / release_id / "release.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(target)
    latest = output_root / "latest.json"
    latest.write_text(
        json.dumps(
            {"release_id": release_id, "manifest": target.relative_to(ROOT).as_posix()},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return target


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=BACKEND / "evidence" / "releases")
    args = parser.parse_args()
    print(build(args.output))
