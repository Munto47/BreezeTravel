"""Read-only, redacted inventory before updating the existing BreezeTravel host.

Run with ``python3 -`` over the existing SSH connection. This script does not
copy credentials, call travel providers, migrate data, or alter any service.
"""

import argparse
import json
import platform
from pathlib import Path
import shutil
import subprocess
from urllib.parse import urlsplit
from urllib.request import urlopen


def command(*args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=20)
    if result.returncode:
        raise RuntimeError("Read-only command failed")
    return result.stdout.strip()


def inspect(name):
    try:
        return json.loads(command("docker", "inspect", name))[0]
    except (RuntimeError, ValueError):
        return None


def file_state(path):
    return {"present": path.is_file(), "bytes": path.stat().st_size if path.is_file() else None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-host", default="iZbp12kpho9obrs2n1564gZ")
    args = parser.parse_args()
    if platform.node() != args.expected_host:
        raise SystemExit("Unexpected host; no inventory performed")
    root = Path("/opt/breezetravel-releases/first-public-20260906")
    api = inspect("breeze-first-api")
    if api is None:
        raise SystemExit("Known application container unavailable; inspect release selection")
    values = dict(item.split("=", 1) for item in api["Config"].get("Env", []) if "=" in item)
    database = urlsplit(values.get("DATABASE_URL", ""))
    dbname = database.path.lstrip("/")
    if not dbname or not all(c.isalnum() or c == "_" for c in dbname):
        raise SystemExit("Unexpected database name; no database queries performed")
    report = {"host": platform.node(), "read_only": True, "containers": {}}
    for name in ("breeze-first-api", "breeze-first-web", "breeze-first-yjs",
                 "breezetravel-postgres-1", "breezetravel-redis-1",
                 "breezetravel-backend-1", "breezetravel-y-websocket-1"):
        item = inspect(name)
        report["containers"][name] = None if item is None else {
            "running": item["State"]["Running"], "started_at": item["State"]["StartedAt"],
            "restart_count": item["RestartCount"],
            "restart_policy": item["HostConfig"]["RestartPolicy"]["Name"],
            "memory_limit_bytes": item["HostConfig"]["Memory"],
            "release": item["Config"].get("Labels", {}).get("breeze.release"),
        }
    report["configuration"] = {
        "database_host": database.hostname, "database": dbname,
        "redis_database": urlsplit(values.get("REDIS_URL", "")).path,
        "modes": {key: values.get(key) for key in (
            "RUNTIME_PROFILE", "TRIP_UNDERSTANDING_PROVIDER_MODE", "AMAP_MOCK",
            "DEMO_MODE", "AUTO_MIGRATE", "REQUIRE_SCHEMA_CHECK", "EXPERIENCE_WORKERS_ENABLED")},
        "credential_presence": {key: bool(values.get(key)) for key in (
            "JWT_SECRET_KEY", "QWEN_API_KEY", "AMAP_API_KEY",
            "TRIP_UNDERSTANDING_COOKIE_SIGNING_KEY", "TRIP_UNDERSTANDING_SOURCE_ENCRYPTION_KEY")},
    }
    def sql(query):
        return command("docker", "exec", "breezetravel-postgres-1", "psql", "-X", "-q", "-U",
                       "postgres", "-d", dbname, "-At", "-v", "ON_ERROR_STOP=1", "-c",
                       "BEGIN READ ONLY; SET LOCAL statement_timeout='5s'; " + query + "; ROLLBACK;")
    report["database"] = {
        "server_version": sql("SHOW server_version"),
        "bytes": int(sql("SELECT pg_database_size(current_database())")),
        "migrations": sql("SELECT filename FROM applied_migrations ORDER BY filename").splitlines(),
        "retained_databases": sql("SELECT datname FROM pg_database WHERE datname LIKE 'breeze%' OR datname='travel_agent' ORDER BY datname").splitlines(),
    }
    report["health"] = {}
    for label, url in (("api", "http://127.0.0.1:8018/health"),
                       ("web", "http://127.0.0.1:3118/"), ("yjs", "http://127.0.0.1:1246/health"),
                       ("public_api", "https://www.breezetravel.cn/health")):
        try:
            with urlopen(url, timeout=5) as response:
                report["health"][label] = {"status": response.status}
        except Exception:
            report["health"][label] = {"status": None, "error": "REQUEST_FAILED"}
    report["release"] = {
        "root": str(root), "source_git_present": (root / "src/.git").exists(),
        "activation_marker": file_state(root / "activated.json"),
        "five_city_module_present": (root / "src/backend/app/trip_understanding/city_knowledge.py").is_file(),
        "private_config_files": {name: file_state(root / name) for name in (
            "private-api.env", "private-web.env", "private-yjs.env")},
    }
    yjs = root / "yjs-data"
    report["yjs"] = {"present": yjs.is_dir(), "bytes": int(command("du", "-sb", str(yjs)).split()[0]) if yjs.is_dir() else None}
    backup = Path("/opt/breezetravel-backups/first-release-20260906")
    report["previous_recovery_assets"] = {name: file_state(backup / name) for name in (
        "travel_agent.dump", "yjs.tar", "breezetravel.cn", "private-old.env",
        "private-container-state.json", "cutover/travel_agent.dump", "cutover/travel-nginx.conf")}
    memory = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
    report["capacity"] = {"memory_kib": {key: int(memory[key].split()[0]) for key in (
        "MemTotal", "MemAvailable", "SwapTotal")}, "disk_free_bytes": shutil.disk_usage(root).free}
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except (OSError, KeyError, ValueError, RuntimeError, subprocess.TimeoutExpired):
        raise SystemExit("Read-only inventory incomplete; private command output suppressed") from None
