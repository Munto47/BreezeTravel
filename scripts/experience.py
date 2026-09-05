"""Start an isolated local text experience without changing an existing database."""
from __future__ import annotations

import argparse
import asyncio
import ctypes
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import time
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
LOCAL = ROOT / ".local-artifacts" / "experience"
STATE = LOCAL / "state.json"
ENV_FILE = LOCAL / "experience.env"
TOOLS = ROOT.parent / "BreezeTravel-G07-Tools"
API_PORT, WEB_PORT, PG_PORT, REDIS_PORT = 8006, 3106, 55439, 56389
HIDDEN = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def read_env(path: Path) -> dict[str, str]:
    from dotenv import dotenv_values
    return {key: value for key, value in dotenv_values(path).items() if value is not None} if path.exists() else {}


def configure() -> dict[str, str]:
    LOCAL.mkdir(parents=True, exist_ok=True)
    if ENV_FILE.exists():
        values = read_env(ENV_FILE)
        if not values.get("TRIP_UNDERSTANDING_QWEN_MODEL"):
            values["TRIP_UNDERSTANDING_QWEN_MODEL"] = selected_model()
            write_env(values)
        return values
    discovered: dict[str, str] = {}
    for source in (ROOT.parent / "BreezeTravel" / ".env", TOOLS / "g07-live.env", ROOT / ".env"):
        discovered.update({k: v for k, v in read_env(source).items() if v})
    values = {
        "RUNTIME_PROFILE": "local_real", "DEMO_MODE": "false", "AMAP_MOCK": "false",
        "TRIP_UNDERSTANDING_PROVIDER_MODE": "live", "EXPERIENCE_WORKERS_ENABLED": "true",
        "AUTO_MIGRATE": "false", "REQUIRE_SCHEMA_CHECK": "true",
        "CHECKPOINT_BOOTSTRAP_ON_START": "false", "LEGACY_IMPORT_DIAGNOSTICS_ENABLED": "false",
        "LANGCHAIN_TRACING_V2": "false", "LANGSMITH_TRACING": "false",
        "TRIP_UNDERSTANDING_QWEN_DEADLINE_SECONDS": "30",
        "TRIP_UNDERSTANDING_QWEN_MAX_OUTPUT_TOKENS": "4096",
        "JWT_SECRET_KEY": secrets.token_urlsafe(36),
        "TRIP_UNDERSTANDING_COOKIE_SIGNING_KEY": secrets.token_urlsafe(36),
        "TRIP_UNDERSTANDING_SOURCE_ENCRYPTION_KEY": secrets.token_urlsafe(36),
        "EXPERIENCE_PG_PASSWORD": secrets.token_urlsafe(24),
        "EXPERIENCE_REDIS_PASSWORD": secrets.token_urlsafe(24),
        "EXPERIENCE_DATABASE": "breezetravel_experience",
    }
    aliases = {
        "QWEN_API_KEY": ("QWEN_API_KEY", "DASHSCOPE_API_KEY"),
        "QWEN_API_URL": ("QWEN_API_URL", "QWEN_BASE_URL"),
        "AMAP_API_KEY": ("AMAP_API_KEY", "AMAP_WEB_SERVICE_KEY"),
        "NEXT_PUBLIC_AMAP_KEY": ("NEXT_PUBLIC_AMAP_KEY", "NEXT_PUBLIC_AMAP_JS_KEY", "AMAP_JS_KEY"),
        "NEXT_PUBLIC_AMAP_SECURITY_CODE": ("NEXT_PUBLIC_AMAP_SECURITY_CODE", "AMAP_JS_SECURITY_CODE"),
        "TRIP_UNDERSTANDING_QWEN_MODEL": ("TRIP_UNDERSTANDING_QWEN_MODEL",),
        "TRIP_UNDERSTANDING_QWEN_INPUT_CNY_PER_MILLION": ("TRIP_UNDERSTANDING_QWEN_INPUT_CNY_PER_MILLION",),
        "TRIP_UNDERSTANDING_QWEN_OUTPUT_CNY_PER_MILLION": ("TRIP_UNDERSTANDING_QWEN_OUTPUT_CNY_PER_MILLION",),
    }
    for key, names in aliases.items():
        values[key] = next((os.environ.get(name) or discovered.get(name) for name in names if os.environ.get(name) or discovered.get(name)), "")
    values["QWEN_API_URL"] = values["QWEN_API_URL"] or "https://dashscope.aliyuncs.com/compatible-mode/v1"
    values["TRIP_UNDERSTANDING_QWEN_MODEL"] = values["TRIP_UNDERSTANDING_QWEN_MODEL"] or selected_model()
    write_env(values)
    return values


def selected_model() -> str:
    path = ROOT / "backend/eval_data/trip_text_cards_agent_v2/qwen_dev_validation_comparison.json"
    return str(json.loads(path.read_text(encoding="utf-8"))["selected_model"])


def write_env(values: dict[str, str]) -> None:
    # JSON quoted dotenv values preserve whitespace without shell interpolation.
    ENV_FILE.write_text("".join(f"{k}={json.dumps(v)}\n" for k, v in values.items()), encoding="utf-8")
    if os.name != "nt":
        ENV_FILE.chmod(0o600)


def environment(values: dict[str, str]) -> dict[str, str]:
    env = os.environ.copy()
    for key, value in values.items():
        if value:
            env[key] = value
        else:
            env.pop(key, None)
    env.update({
        "DATABASE_URL": f"postgresql+asyncpg://experience:{values['EXPERIENCE_PG_PASSWORD']}@127.0.0.1:{PG_PORT}/{values['EXPERIENCE_DATABASE']}",
        "REDIS_URL": f"redis://:{values['EXPERIENCE_REDIS_PASSWORD']}@127.0.0.1:{REDIS_PORT}/0",
        "PGPASSWORD": values["EXPERIENCE_PG_PASSWORD"],
        "BACKEND_INTERNAL_URL": f"http://127.0.0.1:{API_PORT}",
        "NEXT_PUBLIC_API_URL": "", "PYTHONUNBUFFERED": "1", "PYTHONUTF8": "1",
        "LANGCHAIN_TRACING_V2": "false", "LANGSMITH_TRACING": "false",
    })
    env.pop("LANGCHAIN_API_KEY", None)
    return env


def binaries() -> tuple[Path, Path, str]:
    pg = Path(os.environ.get("EXPERIENCE_POSTGRES_BIN", TOOLS / "postgres16-pgvector" / "bin"))
    cache = Path(os.environ.get("EXPERIENCE_REDIS_BIN", TOOLS / "memurai-4.1.2-portable" / "Memurai" / "memurai.exe"))
    node = os.environ.get("EXPERIENCE_NODE") or shutil.which("node")
    if not (pg / "pg_ctl.exe").exists() or not cache.exists() or not node:
        raise RuntimeError("Set EXPERIENCE_POSTGRES_BIN, EXPERIENCE_REDIS_BIN and EXPERIENCE_NODE, or use compose.experience.yml.")
    return pg, cache, node


def load_state() -> dict:
    return json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {"processes": {}}


def save_state(state: dict) -> None:
    LOCAL.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def process_stamp(pid: int) -> int | None:
    if os.name != "nt":
        path = Path(f"/proc/{pid}/stat")
        return int(path.read_text().split()[21]) if path.exists() else None
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.restype = ctypes.c_void_p
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return None
    times = [ctypes.c_ulonglong() for _ in range(4)]
    try:
        ok = kernel.GetProcessTimes(ctypes.c_void_p(handle), *(ctypes.byref(t) for t in times))
        return times[0].value if ok else None
    finally:
        kernel.CloseHandle(ctypes.c_void_p(handle))


def running(record: dict | None) -> bool:
    return bool(record and process_stamp(record["pid"]) == record["stamp"] and record["stamp"] is not None)


def launch(name: str, args: list[str], cwd: Path, env: dict, state: dict) -> None:
    if running(state["processes"].get(name)):
        return
    with (LOCAL / f"{name}.log").open("ab") as log:
        process = subprocess.Popen(args, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log, creationflags=HIDDEN)
    state["processes"][name] = {"pid": process.pid, "stamp": process_stamp(process.pid)}
    save_state(state)


def web_environment(env: dict[str, str]) -> dict[str, str]:
    allowed = {"path", "systemroot", "windir", "comspec", "pathext", "appdata", "localappdata", "userprofile", "home", "temp", "tmp", "programfiles", "programfiles(x86)", "volta_home", "http_proxy", "https_proxy", "no_proxy", "backend_internal_url", "next_public_api_url", "next_public_amap_key", "next_public_amap_security_code"}
    return {
        **{key: value for key, value in env.items() if key.lower() in allowed},
        "NEXT_TELEMETRY_DISABLED": "1",
        "EXPERIENCE_WEB_RUNTIME": "1",
    }


def command(args: list[str], env: dict, *, cwd: Path = ROOT) -> None:
    # PostgreSQL children can inherit stdout on Windows: a PIPE would never EOF.
    with (LOCAL / "operations.log").open("ab") as log:
        result = subprocess.run(args, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log, creationflags=HIDDEN)
    if result.returncode:
        # Never echo a command's credentials or provider response.
        raise RuntimeError(f"Local operation failed: {Path(args[0]).name}. See the private runtime logs.")


def port_ready(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.3)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def wait_ready(port: int, *, http_path: str | None = None, seconds: int = 45) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            if http_path is not None:
                with urlopen(f"http://127.0.0.1:{port}{http_path}", timeout=2) as response:
                    if response.status == 200:
                        return
            elif port_ready(port):
                return
        except Exception:
            pass
        time.sleep(0.3)
    raise RuntimeError(f"Local service on port {port} did not become ready; private logs retained.")


def stop_process(name: str, state: dict) -> None:
    record = state["processes"].get(name)
    if running(record):
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(record["pid"]), "/T", "/F"], capture_output=True, creationflags=HIDDEN)
        else:
            os.kill(record["pid"], 15)
    state["processes"].pop(name, None)
    save_state(state)


def start_web(node: str, env: dict[str, str], state: dict, *, dev: bool = False) -> None:
    if not running(state["processes"].get("web")) and port_ready(WEB_PORT):
        raise RuntimeError("Web port is occupied; existing service left untouched")
    stop_process("web", state)
    frontend = ROOT / "frontend"
    next_cli = str(frontend / "node_modules/next/dist/bin/next")
    child_env = {**web_environment(env), "NODE_ENV": "development" if dev else "production"}
    if not dev:
        # Always rebuild after stopping the previous web process: current source and
        # browser configuration must not be served from an older build.
        print("Building the current web experience...")
        command([node, next_cli, "build"], child_env, cwd=frontend)
    launch("web", [node, next_cli, "dev" if dev else "start", "--hostname", "127.0.0.1", "--port", str(WEB_PORT)], frontend, child_env, state)
    state["web_mode"] = "development" if dev else "production"
    save_state(state)
    wait_ready(WEB_PORT, http_path="/")


async def migrate(values: dict[str, str], *, dsn: str | None = None) -> None:
    import asyncpg
    dsn = (dsn or environment(values)["DATABASE_URL"]).replace("postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute("SELECT pg_advisory_lock(31068006)")
        await conn.execute((ROOT / "backend/app/db/init.sql").read_text(encoding="utf-8"))
        await conn.execute("CREATE TABLE IF NOT EXISTS applied_migrations (filename TEXT PRIMARY KEY, applied_at TIMESTAMPTZ DEFAULT NOW())")
        applied = {row["filename"] for row in await conn.fetch("SELECT filename FROM applied_migrations")}
        for path in sorted((ROOT / "backend/app/db/migrations").glob("*.sql")):
            if path.name not in applied:
                async with conn.transaction():
                    await conn.execute(path.read_text(encoding="utf-8"))
                    await conn.execute("INSERT INTO applied_migrations(filename) VALUES($1)", path.name)
    finally:
        await conn.close()


async def ensure_database(values: dict[str, str]) -> None:
    import asyncpg
    connection = await asyncpg.connect(host="127.0.0.1", port=PG_PORT, user="experience", password=values["EXPERIENCE_PG_PASSWORD"], database="postgres")
    name = values["EXPERIENCE_DATABASE"]
    if not name.replace("_", "").isalnum():
        raise RuntimeError("Invalid private database name")
    try:
        if not await connection.fetchval("SELECT EXISTS(SELECT 1 FROM pg_database WHERE datname=$1)", name):
            await connection.execute(f'CREATE DATABASE "{name}"')
    finally:
        await connection.close()


def start(*, no_web: bool = False, dev: bool = False) -> None:
    values = configure()
    missing = [name for name in ("QWEN_API_KEY", "AMAP_API_KEY", "TRIP_UNDERSTANDING_QWEN_MODEL") if not values.get(name)]
    if missing:
        raise RuntimeError("Existing provider configuration is missing: " + ", ".join(missing))
    env = environment(values)
    pg, cache, node = binaries()
    state = load_state()
    data = LOCAL / "postgres"
    if not (data / "PG_VERSION").exists():
        if data.exists() and any(data.iterdir()):
            raise RuntimeError("Unrecognized data directory retained; refusing to initialize over it")
        password_file = LOCAL / "postgres-password.txt"
        password_file.write_text(values["EXPERIENCE_PG_PASSWORD"], encoding="utf-8")
        command([str(pg / "initdb.exe"), "-D", str(data), "-U", "experience", "-A", "scram-sha-256", "--encoding=UTF8", "--locale=C", f"--pwfile={password_file}"], env)
        password_file.unlink()
    status = subprocess.run([str(pg / "pg_ctl.exe"), "-D", str(data), "status"], capture_output=True, creationflags=HIDDEN)
    if status.returncode:
        if port_ready(PG_PORT):
            raise RuntimeError("Private PostgreSQL port is occupied; existing service left untouched")
        command([str(pg / "pg_ctl.exe"), "-D", str(data), "-l", str(LOCAL / "postgres.log"), "-o", f"-p {PG_PORT} -h 127.0.0.1", "-w", "start"], env)
    wait_ready(PG_PORT)
    state["postgres_data"] = str(data)
    state["postgres_bin"] = str(pg)
    save_state(state)
    asyncio.run(ensure_database(values))
    asyncio.run(migrate(values))
    if not running(state["processes"].get("redis")):
        if port_ready(REDIS_PORT):
            raise RuntimeError("Private Redis port is occupied; existing service left untouched")
        redis_data = LOCAL / "redis"
        redis_data.mkdir(exist_ok=True)
        config = LOCAL / "redis.conf"
        config.write_text(f'bind 127.0.0.1\nport {REDIS_PORT}\nprotected-mode yes\nrequirepass {values["EXPERIENCE_REDIS_PASSWORD"]}\ndir "{redis_data.as_posix()}"\nsave ""\nappendonly no\n', encoding="utf-8")
        launch("redis", [str(cache), str(config)], LOCAL, env, state)
    wait_ready(REDIS_PORT)
    if not running(state["processes"].get("api")) and port_ready(API_PORT):
        raise RuntimeError("API port is occupied; existing service left untouched")
    launch("api", [sys.executable, "-m", "uvicorn", "app.experience_main:app", "--host", "127.0.0.1", "--port", str(API_PORT), "--no-access-log"], ROOT / "backend", env, state)
    wait_ready(API_PORT, http_path="/health")
    if not no_web:
        start_web(node, env, state, dev=dev)
    print(f"API ready: http://127.0.0.1:{API_PORT}; Web: {'not started' if no_web else f'http://127.0.0.1:{WEB_PORT}'}")


def stop(*, applications_only: bool = False) -> None:
    state = load_state()
    names = ["web", "api"] if applications_only else ["web", "api", "redis"]
    for name in names:
        stop_process(name, state)
    if not applications_only and state.get("postgres_data"):
        data = Path(state["postgres_data"]).resolve()
        if data != (LOCAL / "postgres").resolve():
            raise RuntimeError("Refusing to stop a database outside this runtime")
        subprocess.run([str(Path(state["postgres_bin"]) / "pg_ctl.exe"), "-D", str(data), "-m", "fast", "-w", "stop"], capture_output=True, creationflags=HIDDEN)
    save_state(state)
    print("Local services stopped; database and secrets retained.")


def backup() -> None:
    values = configure()
    pg, _, _ = binaries()
    target = LOCAL / f"backup-{time.strftime('%Y%m%d-%H%M%S')}.dump"
    command([str(pg / "pg_dump.exe"), "-h", "127.0.0.1", "-p", str(PG_PORT), "-U", "experience", "-d", values["EXPERIENCE_DATABASE"], "-Fc", "-f", str(target)], environment(values))
    print(f"Private backup saved: {target}")


def restore(path: Path, *, no_web: bool = False, dev: bool = False) -> None:
    backup_file = path.resolve(strict=True)
    if not backup_file.is_file() or backup_file.suffix != ".dump":
        raise RuntimeError("Choose an existing private .dump backup")
    values = configure()
    restored = {**values, "EXPERIENCE_DATABASE": f"breeze_restore_{time.strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(3)}"}
    pg, _, _ = binaries()
    stop(applications_only=True)
    try:
        asyncio.run(ensure_database(restored))
        command([str(pg / "pg_restore.exe"), "-h", "127.0.0.1", "-p", str(PG_PORT), "-U", "experience", "-d", restored["EXPERIENCE_DATABASE"], "--exit-on-error", str(backup_file)], environment(restored))
        asyncio.run(migrate(restored))
        write_env(restored)
    except Exception:
        start(no_web=no_web, dev=dev)
        raise
    start(no_web=no_web, dev=dev)
    print("Backup restored into a new database; previous database retained.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["start", "stop", "restart", "status", "backup", "restore", "migrate", "configure"])
    parser.add_argument("--no-web", action="store_true")
    parser.add_argument("--dev", action="store_true", help="Opt into the development web server instead of a fresh production build")
    parser.add_argument("--backup-file", type=Path)
    args = parser.parse_args()
    if args.action == "start":
        start(no_web=args.no_web, dev=args.dev)
    elif args.action == "stop":
        stop()
    elif args.action == "restart":
        stop(applications_only=True)
        start(no_web=args.no_web, dev=args.dev)
    elif args.action == "backup":
        backup()
    elif args.action == "restore":
        if args.backup_file is None:
            raise RuntimeError("restore requires --backup-file")
        restore(args.backup_file, no_web=args.no_web, dev=args.dev)
    elif args.action == "configure":
        configure()
        print("Private configuration is ready; no secrets printed.")
    elif args.action == "migrate":
        asyncio.run(migrate(configure()))
    else:
        state = load_state()
        print(json.dumps({"api": running(state["processes"].get("api")), "web": running(state["processes"].get("web")), "web_mode": state.get("web_mode", "unknown"), "postgres": port_ready(PG_PORT), "redis": running(state["processes"].get("redis"))}))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Dependency failures can include DSNs; report only controlled errors.
        print(str(exc) if isinstance(exc, RuntimeError) else f"Local runtime operation failed ({type(exc).__name__}); existing data retained.", file=sys.stderr)
        sys.exit(1)
