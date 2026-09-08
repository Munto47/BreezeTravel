"""Upgrade this existing host through a private rehearsal and an explicit cutover.

Without --execute every action is a read-only plan. Source is supplied with
``git archive``; credentials and data stay on the server. Rehearsal never becomes
the live database. Recovery after possible public writes keeps the new data.
The previous deployed code cannot read current public records; recovery uses
this release's code. A subsequent code fix must use a new explicit release.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import socket
import subprocess
import tarfile
import time
from urllib.parse import urlsplit, urlunsplit
from urllib.request import urlopen


class UpgradeError(RuntimeError):
    pass


# Trial ceilings: the 2026-09-08 controlled Windows build peaked at 783.0 MiB with
# one static worker and a separate webpack worker. Current Linux process
# high-water RSS is API 346 / web 94 / Yjs 59 MiB. These bounds protect the
# existing host during the Linux trial; they are not measured workload demand.
BUILD_MEMORY_MIB = 1024
RUNTIME_MEMORY_MIB = {"api": 640, "web": 192, "yjs": 128}
MEMORY_RESERVE_MIB = 384


def has_live_model_call(bindings: list, expected_model: str) -> bool:
    """Read actual call metadata, independent of a supplier's adapter label."""
    if not isinstance(expected_model, str) or not expected_model.strip():
        return False
    for binding in bindings:
        if not isinstance(binding, dict) or binding.get("model") != expected_model:
            continue
        count, calls = binding.get("external_calls"), binding.get("calls")
        if type(count) is not int or count <= 0 or not isinstance(calls, list) or len(calls) != count:
            continue
        if any(isinstance(call, dict) and isinstance(call.get("reported_model"), str)
               and call["reported_model"].strip() for call in calls):
            return True
    return False


def identifier(value: str) -> str:
    if not re.fullmatch(r"[a-z][a-z0-9_]{2,55}", value):
        raise UpgradeError("Invalid database name")
    return value


def checked_release(value: str) -> Path:
    structure = PurePosixPath(value)
    path = Path(value)
    if not structure.is_absolute() or structure.parent != PurePosixPath("/opt/breezetravel-releases"):
        raise UpgradeError("Release must be a direct child of /opt/breezetravel-releases")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{2,70}", path.name) or path.is_symlink():
        raise UpgradeError("Invalid release directory")
    if path.exists() and path.resolve() != path:
        raise UpgradeError("Release path resolves elsewhere")
    if platform.system() == "Linux" and path.parent.exists() and path.parent.resolve() != path.parent:
        raise UpgradeError("Release parent resolves elsewhere")
    return path


def database_url(original: str, name: str) -> str:
    parsed = urlsplit(original)
    if parsed.scheme not in {"postgresql", "postgresql+asyncpg"} or not parsed.hostname:
        raise UpgradeError("Unexpected database connection type")
    return urlunsplit((parsed.scheme, parsed.netloc, "/" + identifier(name), parsed.query, ""))


def extract_source(archive: Path, target: Path) -> None:
    """Only ordinary Git archive files; no links, credentials or parent traversal."""
    with tarfile.open(archive) as source:
        members = source.getmembers()
        for member in members:
            path = PurePosixPath(member.name)
            if (path.is_absolute() or ".." in path.parts or "\\" in member.name
                    or not (member.isfile() or member.isdir())
                    or any(p in {".git", "node_modules", ".local-artifacts"}
                           or (p.startswith(".env") and not p.endswith(".example")) for p in path.parts)):
                raise UpgradeError("Source archive contains an unsafe entry")
        source.extractall(target, members=members, filter="data")
    for name in ("backend/app/experience_main.py", "scripts/experience.py",
                 "frontend/package-lock.json", "y-websocket/server.js"):
        if not (target / name).is_file():
            raise UpgradeError("Source archive lacks required runtime files")


def replace_upstreams(config: str, ports: dict[int, int]) -> str:
    for old in ports:
        if not re.search(rf"proxy_pass\s+http://127\.0\.0\.1:{old}(?=[/;\s])", config):
            raise UpgradeError("Expected nginx upstream absent")
    pattern = re.compile(r"(proxy_pass\s+http://127\.0\.0\.1:)(\d+)(?=[/;\s])")
    return pattern.sub(lambda m: m[1] + str(ports.get(int(m[2]), int(m[2]))), config)


def save_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2), encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


class Upgrade:
    def __init__(self, args):
        self.args = args
        self.current = checked_release(args.current_release)
        self.target = checked_release(args.new_release)
        if self.current == self.target:
            raise UpgradeError("A new release directory is required")
        identifier(args.current_db)
        identifier(args.new_db)
        identifier(args.rehearsal_db)
        if len({args.current_db, args.new_db, args.rehearsal_db}) != 3:
            raise UpgradeError("Current, rehearsal and new databases must be distinct")
        if not math.isfinite(args.model_deadline_seconds) or args.model_deadline_seconds <= 0 or args.model_max_output_tokens <= 0:
            raise UpgradeError("Explicit positive model limits are required")
        self.ports = {"api": args.api_port, "web": args.web_port, "yjs": args.yjs_port}
        if len(set(self.ports.values())) != 3 or any(not 1024 <= p <= 65535 for p in self.ports.values()):
            raise UpgradeError("Three distinct unprivileged ports are required")
        self.names = {kind: f"{self.target.name}-{kind}" for kind in self.ports}
        self.old_names = {"api": args.current_api, "web": args.current_web, "yjs": args.current_yjs}
        if set(self.names.values()) & set(self.old_names.values()):
            raise UpgradeError("New container names overlap current containers")
        self.state_path = self.target / "upgrade-state.json"
        self.state = {}
        self.containers = {}
        self.environment = {}
        self.mutation_started = False
        if not 0 <= args.preview_redis_db <= 15 or args.preview_redis_db == args.redis_db:
            raise UpgradeError("Preview and live Redis databases must be distinct")

    def run(self, *args, stdin=None, stdout=None, timeout=120) -> str:
        try:
            result = subprocess.run(args, stdin=stdin, stdout=stdout or subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=stdin is None and stdout is None,
                                    timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise UpgradeError(self.failure_log(exc.stderr, "timeout")) from None
        if result.returncode:
            raise UpgradeError(self.failure_log(result.stderr, f"exit {result.returncode}"))
        return result.stdout.strip() if stdout is None else ""

    def failure_log(self, stderr, outcome: str) -> str:
        """Keep diagnostics private; a read-only plan never creates a file."""
        phase = self.state.get("phase", "PREFLIGHT")
        summary = f"Command failed ({outcome}); phase={phase}"
        if not self.args.execute or not self.target.is_dir() or self.target.is_symlink():
            return summary + "; private log unavailable before release preparation"
        message = stderr.decode("utf-8", errors="replace") if isinstance(stderr, bytes) else stderr or "(no stderr)"
        # Commands never receive source text. Mask configuration values and common
        # credential formats in dependency errors without losing the stack trace.
        secrets = set()
        environments = [self.environment, *(self.env(c) for c in self.containers.values() if "Config" in c)]
        for environment in environments:
            for key, value in environment.items():
                if re.search(r"SECRET|PASSWORD|TOKEN|(?:^|_)KEY|SECURITY_CODE|DATABASE_URL", key):
                    if value:
                        secrets.add(value)
                        if "://" in value and urlsplit(value).password:
                            secrets.add(urlsplit(value).password)
        for secret in sorted(secrets, key=len, reverse=True):
            message = message.replace(secret, "[REDACTED]")
        message = re.sub(r"(?i)(?:postgres(?:ql)?|redis)://[^\s\"']+", "[REDACTED_CONNECTION]", message)
        message = re.sub(r"(?i)(bearer\s+)\S+", r"\1[REDACTED]", message)
        path = self.target / f"private-failure-{time.time_ns()}.log"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(summary + "\n" + message + "\n")
        return summary + f"; private_log={path}"

    def inspect(self, name: str) -> dict:
        return json.loads(self.run("docker", "inspect", name))[0]

    def failure_detail(self, error: BaseException) -> str:
        if isinstance(error, UpgradeError):
            return str(error)
        return self.failure_log(str(error), type(error).__name__)

    @staticmethod
    def env(container: dict) -> dict:
        return dict(item.split("=", 1) for item in container["Config"].get("Env", []) if "=" in item)

    def query(self, db: str, sql: str) -> str:
        return self.run("docker", "exec", self.args.postgres_container, "psql", "-X", "-q",
                        "-U", "postgres", "-d", identifier(db), "-At", "-v", "ON_ERROR_STOP=1", "-c",
                        "BEGIN READ ONLY; SET LOCAL statement_timeout='5s'; " + sql + "; ROLLBACK;")

    def snapshot(self) -> dict:
        if platform.node() != self.args.expected_host:
            raise UpgradeError("Unexpected host")
        self.current_source = self.current / "src"
        current_state = self.current / "upgrade-state.json"
        if current_state.is_file():
            selected = json.loads(current_state.read_text()).get("live_source")
            if selected:
                selected = Path(selected)
                checked_release(str(selected.parent))
                if selected.name != "src":
                    raise UpgradeError("Previous release source location is invalid")
                self.current_source = selected
        for kind, name in self.old_names.items():
            container = self.inspect(name)
            self.containers[kind] = container
            expected = self.current_source / ("backend" if kind == "api" else "frontend/.next/standalone" if kind == "web" else "y-websocket/server.js")
            if str(expected) not in {m["Source"] for m in container["Mounts"]}:
                raise UpgradeError("Current container does not mount the expected release")
        self.environment = self.env(self.containers["api"])
        if urlsplit(self.environment.get("DATABASE_URL", "")).path != "/" + self.args.current_db:
            raise UpgradeError("Current API database differs from explicit target")
        if str(self.current / "yjs-data") not in {m["Source"] for m in self.containers["yjs"]["Mounts"] if m["Destination"] == "/data"}:
            raise UpgradeError("Current collaboration data path differs")
        redis = urlsplit(self.environment.get("REDIS_URL", ""))
        if not 0 <= self.args.redis_db <= 15 or redis.path in {"/" + str(self.args.redis_db), "/" + str(self.args.preview_redis_db)}:
            raise UpgradeError("A separate valid Redis database is required")
        for name in self.names.values():
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]+", name):
                raise UpgradeError("Invalid application container name")
        if not self.args.nginx_site.is_file() or self.args.nginx_site.is_symlink():
            raise UpgradeError("Expected nginx site must be a regular configuration file")
        if self.state_path.exists():
            self.state = json.loads(self.state_path.read_text())
            expected = {"host": self.args.expected_host, "previous_release": str(self.current),
                        "previous_database": self.args.current_db, "live_database": self.args.new_db,
                        "containers": self.names, "ports": self.ports,
                        "redis_databases": [self.args.redis_db, self.args.preview_redis_db],
                        "model_limits": [self.args.model_deadline_seconds, self.args.model_max_output_tokens]}
            if any(self.state.get(k) != v for k, v in expected.items()):
                raise UpgradeError("Saved operation targets differ from supplied arguments")
        current_ports = {}
        for kind, container in self.containers.items():
            bindings = container["HostConfig"]["PortBindings"]
            matches = [int(b["HostPort"]) for rows in bindings.values() for b in rows or [] if b["HostIp"] == "127.0.0.1"]
            if len(matches) != 1 or matches[0] in self.ports.values():
                raise UpgradeError("Unexpected or overlapping current port mapping")
            current_ports[kind] = matches[0]
        networks = list(self.containers["api"]["NetworkSettings"]["Networks"])
        if len(networks) != 1:
            raise UpgradeError("Current API must use one explicit Docker network")
        self.network = networks[0]
        self.current_ports = current_ports
        if not self.state.get("traffic_may_have_switched"):
            replace_upstreams(self.args.nginx_site.read_text(), {current_ports[k]: self.ports[k] for k in self.ports})
        self.assert_writer_set(self.args.current_db, {self.args.current_api})
        browser = {}
        for line in (self.current / "private-web.env").read_text().splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                browser[key] = value.strip().strip('"')
        return {"host": self.args.expected_host, "current_release": str(self.current),
                "current_database": self.args.current_db, "new_release": str(self.target),
                "new_database": self.args.new_db, "rehearsal_database": self.args.rehearsal_db,
                "ports": self.ports, "phase": self.state.get("phase", "UNPREPARED"), "source_ref": self.state.get("source_ref"),
                "migrations": self.query(self.args.current_db, "SELECT filename FROM applied_migrations ORDER BY filename").splitlines(),
                "disk_free_bytes": shutil.disk_usage(self.current).free,
                "memory_available_kib": self.available_memory_kib(),
                "trial_container_memory_mib": {"build": BUILD_MEMORY_MIB, **RUNTIME_MEMORY_MIB},
                "memory_reserve_mib": MEMORY_RESERVE_MIB,
                "build_start_available_memory_kib": (BUILD_MEMORY_MIB + MEMORY_RESERVE_MIB) * 1024,
                "preview_start_available_memory_kib": (sum(RUNTIME_MEMORY_MIB.values()) + MEMORY_RESERVE_MIB) * 1024,
                "memory_interpretation": "Trial ceilings and host reserve, not measured demand; Linux build and real preview under these ceilings remain necessary",
                "preview_url": f"http://127.0.0.1:{self.ports['web']}",
                "preview_redis_database": self.args.preview_redis_db,
                "configured_model": {key: self.environment.get(key) for key in (
                    "TRIP_UNDERSTANDING_QWEN_MODEL", "TRIP_UNDERSTANDING_QWEN_DEADLINE_SECONDS", "TRIP_UNDERSTANDING_QWEN_MAX_OUTPUT_TOKENS")},
                "new_model_limits": {"deadline_seconds": self.args.model_deadline_seconds, "max_output_tokens": self.args.model_max_output_tokens},
                "private_setting_presence": {key: bool(self.environment.get(key)) for key in (
                    "JWT_SECRET_KEY", "TRIP_UNDERSTANDING_COOKIE_SIGNING_KEY", "TRIP_UNDERSTANDING_SOURCE_ENCRYPTION_KEY", "AMAP_API_KEY")},
                "browser_setting_presence": {key: bool(browser.get(key)) for key in ("NEXT_PUBLIC_AMAP_KEY", "NEXT_PUBLIC_AMAP_SECURITY_CODE")},
                "site_allowlist": self.environment.get("CORS_ORIGIN_REGEX"),
                "dependency_restart_policies": {n: self.inspect(n)["HostConfig"]["RestartPolicy"]["Name"] for n in (self.args.postgres_container, self.args.redis_container)}}

    def assert_writer_set(self, db: str, allowed: set[str]) -> None:
        ids = self.run("docker", "ps", "-q").splitlines()
        for item in ids:
            container = self.inspect(item)
            name = container["Name"].lstrip("/")
            url = self.env(container).get("DATABASE_URL", "")
            if urlsplit(url).path == "/" + db and name not in allowed:
                raise UpgradeError("An unexpected running container uses the target database")

    def record(self, phase: str, **values) -> None:
        self.mutation_started = True
        self.state.update(phase=phase, **values)
        save_json(self.state_path, self.state)
        print(json.dumps({"phase": phase, "release": self.target.name}), flush=True)

    def assert_free_ports(self) -> None:
        for port in self.ports.values():
            with socket.socket() as probe:
                if probe.connect_ex(("127.0.0.1", port)) == 0:
                    raise UpgradeError("A selected private port is already in use")

    @staticmethod
    def available_memory_kib() -> int:
        memory = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
        return int(memory["MemAvailable"].split()[0])

    def check_resources(self, operation: str) -> None:
        # 384 MiB is an operational reserve above the bounded container budgets,
        # not a promise of workload capacity. Never evict current services to build.
        limit = {"build": BUILD_MEMORY_MIB, "preview": sum(RUNTIME_MEMORY_MIB.values()), "migrate": 900}[operation]
        available = self.available_memory_kib()
        required = (limit + MEMORY_RESERVE_MIB) * 1024
        if available < required:
            raise UpgradeError(f"Insufficient available memory for {operation}: {available} KiB available, {required} KiB required; current services retained")
        database_bytes = int(self.query(self.args.current_db, "SELECT pg_database_size(current_database())"))
        if shutil.disk_usage(self.current).free < 3 * 1024**3 + 4 * database_bytes:
            raise UpgradeError("Insufficient free disk for source/build, snapshots and two database copies")

    def phase(self) -> str:
        return self.state.get("phase", "").removeprefix("FAILED_")

    def check_source_ref(self) -> None:
        if self.state and (self.args.action == "prepare" or self.args.source_ref != "unspecified"):
            if self.state.get("source_ref") != self.args.source_ref:
                raise UpgradeError("Existing release has a different source ref; use a new release directory for new code")

    def assert_redis_unused(self, index: int) -> None:
        for name in self.run("docker", "ps", "-q").splitlines():
            item = self.inspect(name)
            if urlsplit(self.env(item).get("REDIS_URL", "")).path == f"/{index}":
                raise UpgradeError("Selected Redis database is already used by a running container")
        if self.run("docker", "exec", self.args.redis_container, "redis-cli", "-n", str(index), "DBSIZE") != "0":
            raise UpgradeError("Selected Redis database contains data; choose an unused database without flushing it")

    def private_env(self, path: Path, values: dict) -> None:
        if any("\n" in str(v) or "\r" in str(v) for v in values.values()):
            raise UpgradeError("Environment value contains a line break")
        path.write_text("".join(f"{k}={v}\n" for k, v in values.items()), encoding="utf-8")
        os.chmod(path, 0o600)

    def prepare(self) -> None:
        self.check_source_ref()
        if self.phase() in {"PREPARED", "COPY_RESTORED", "PREVIEW_SERVING_UNVERIFIED", "LIVE", "RECOVERED_KEEPING_CURRENT_DATA"}:
            return
        retry_build = self.phase() == "SOURCE_READY"
        if (self.target.exists() and not retry_build) or self.args.source_archive is None:
            raise UpgradeError("Prepare requires a new directory and an explicit source archive")
        if retry_build and self.state.get("source_ref") != self.args.source_ref:
            raise UpgradeError("Build retry must use the same source ref")
        self.check_resources("build")
        self.assert_free_ports()
        self.assert_redis_unused(self.args.redis_db)
        self.assert_redis_unused(self.args.preview_redis_db)
        if not retry_build:
            self.target.mkdir(mode=0o700)
            self.state = {"host": self.args.expected_host, "previous_release": str(self.current),
                      "previous_database": self.args.current_db, "live_database": self.args.new_db,
                      "containers": self.names, "ports": self.ports, "traffic_may_have_switched": False,
                      "redis_databases": [self.args.redis_db, self.args.preview_redis_db],
                      "model_limits": [self.args.model_deadline_seconds, self.args.model_max_output_tokens],
                      "source_ref": self.args.source_ref}
            self.record("PREPARING")
            (self.target / "src").mkdir(mode=0o700)
            extract_source(self.args.source_archive, self.target / "src")
            self.record("SOURCE_READY")
        browser = self.env(self.containers["web"])
        # Build receives browser-public settings only, never the API environment.
        existing = self.current / "private-web.env"
        for line in existing.read_text().splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                try:
                    browser[key] = json.loads(value)
                except ValueError:
                    browser[key] = value
        public = {k: v for k, v in browser.items() if k.startswith("NEXT_PUBLIC_")}
        if not str(public.get("NEXT_PUBLIC_Y_WEBSOCKET_URL", "")).startswith("wss://"):
            raise UpgradeError("Explicit deployed secure collaboration URL is required for the live build")
        public.update(BACKEND_INTERNAL_URL=f"http://{self.names['api']}:8006", EXPERIENCE_WEB_RUNTIME="0",
                      NEXT_TELEMETRY_DISABLED="1", NODE_ENV="production", NEXT_PUBLIC_API_URL="")
        self.private_env(self.target / "private-web.env", public)
        self.build_web(self.target / "src")
        self.record("PREPARED")

    def build_web(self, source: Path, *, preview: bool = False) -> None:
        self.check_resources("build")
        image = self.containers["web"]["Image"]
        frontend = source / "frontend"
        base = ["docker", "run", "--rm", "--name", self.target.name + "-build",
                "--label", f"breeze.upgrade.release={self.target.name}", f"--memory={BUILD_MEMORY_MIB}m", f"--memory-swap={BUILD_MEMORY_MIB}m", "--cpus=1", "--network", "none",
                "-v", f"{source}/frontend:/build", "--env-file", str(self.target / ("private-preview-web.env" if preview else "private-web.env")),
                "-w", "/build", "--entrypoint"]
        # The source is separate; never install into, mount writable, or mutate
        # the live release. Reuse only identical manifests and validate the copy.
        old_frontend = self.current_source / "frontend"
        reusable = all((old_frontend / name).is_file() and (frontend / name).is_file()
                       and json.loads((old_frontend / name).read_text()) == json.loads((frontend / name).read_text())
                       for name in ("package.json", "package-lock.json"))
        if reusable and (old_frontend / "node_modules").is_dir():
            shutil.copytree(old_frontend / "node_modules", frontend / "node_modules", symlinks=True, dirs_exist_ok=True)
            self.run(*base, "npm", image, "ls", "--depth=0", "--include=dev", "--offline", timeout=120)
        else:
            # A dependency change needs a separate download-capable, bounded
            # install. It must not silently inherit old modules.
            install = list(base)
            install[install.index("--network") + 1] = self.network
            self.run(*install, "npm", image, "ci", "--include=dev", "--no-audit", "--no-fund", timeout=1200)
        config_check = """const load=require('next/dist/server/config').default;
load('phase-production-build',process.cwd()).then(c=>{
 if(c.experimental.cpus!==1||c.experimental.webpackBuildWorker!==true)process.exit(2);
}).catch(()=>process.exit(2));"""
        self.run(*base, "node", image, "-e", config_check, timeout=60)
        # Read cgroup peak while the parent process still keeps the container
        # alive. An OOM that kills both processes remains a failure, not a pass.
        measured_build = """const fs=require('fs'),cp=require('child_process');
const result=cp.spawnSync(process.execPath,['node_modules/next/dist/bin/next','build'],{stdio:'inherit'});
const report={exit_code:result.status,signal:result.signal};
for(const [key,path] of Object.entries({peak_bytes:'/sys/fs/cgroup/memory.peak',events:'/sys/fs/cgroup/memory.events'})){
 try{report[key]=fs.readFileSync(path,'utf8').trim()}catch{report[key]=null}
}
fs.writeFileSync('/build/.release-build-memory.json',JSON.stringify(report,null,2),{mode:0o600});
process.exit(result.status===null?1:result.status);"""
        measurement = frontend / ".release-build-memory.json"
        measurement.unlink(missing_ok=True)
        self.run(*base, "node", image, "-e", measured_build, timeout=1200)
        measured = json.loads(measurement.read_text())
        events = dict(line.split() for line in (measured.get("events") or "").splitlines())
        if measured.get("exit_code") != 0 or int(events.get("oom_kill", "0")) > 0:
            raise UpgradeError("Bounded frontend build did not finish without a container OOM")
        print(json.dumps({"phase": "FRONTEND_BUILD_MEASURED", "limit_mib": BUILD_MEMORY_MIB,
                          "peak_bytes": measured.get("peak_bytes"), "events": events}), flush=True)
        if not (source / "frontend/.next/standalone/server.js").is_file():
            raise UpgradeError("Frontend standalone build is missing")

    def remove_new_containers(self, *, remove=True) -> None:
        all_names = set(self.run("docker", "ps", "-a", "--format", "{{.Names}}").splitlines())
        for name in [*self.names.values(), self.target.name + "-build", self.target.name + "-migrate"]:
            if name in all_names:
                item = self.inspect(name)
                if item["Config"].get("Labels", {}).get("breeze.upgrade.release") != self.target.name:
                    raise UpgradeError("Refusing to remove a container not owned by this release")
                self.run("docker", "stop", "--time", "70", name)
                if remove:
                    self.run("docker", "rm", name)

    @contextmanager
    def stopped_previous_writers(self):
        names = [self.args.current_api, self.args.current_yjs]
        if any(not self.inspect(n)["State"]["Running"] for n in names):
            raise UpgradeError("Previous writers must be running before the maintenance snapshot")
        operation_error = None
        try:
            self.run("docker", "stop", "--time", "70", *names)
            self.assert_writer_set(self.args.current_db, set())
            yield
        except BaseException as exc:
            operation_error = exc
            raise
        finally:
            if not self.state.get("traffic_may_have_switched"):
                # Shut down every partially started new writer before old writers return.
                failures = []
                try:
                    self.remove_new_containers()
                except Exception as exc:
                    failures.append(("new writer shutdown; old writers not restarted", exc))
                else:
                    for kind, name in zip(("api", "yjs"), names):
                        try:
                            self.run("docker", "start", name)
                        except Exception as exc:
                            failures.append((f"{kind} start", exc))
                    # A failed start acknowledgement must not prevent checking either service.
                    for kind in ("api", "yjs"):
                        try:
                            self.health(ports={kind: self.current_ports[kind]}, context="Previous service recovery")
                        except Exception as exc:
                            failures.append((f"{kind} health", exc))
                if failures:
                    messages = ([f"Original operation failed: {self.failure_detail(operation_error)}"] if operation_error else [])
                    messages.extend(f"{stage}: {self.failure_detail(error)}" for stage, error in failures)
                    raise UpgradeError("; ".join(messages) + "; previous-service recovery incomplete; data and backups retained") from (
                        operation_error if operation_error is not None else failures[0][1])

    def backup(self, db: str, data: Path, label: str) -> Path:
        folder = self.target / f"backup-{label}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}"
        folder.mkdir(mode=0o700)
        with (folder / "database.dump").open("xb") as output:
            self.run("docker", "exec", self.args.postgres_container, "pg_dump", "-U", "postgres", "-d", identifier(db), "-Fc", stdout=output)
        with (folder / "database.dump").open("rb") as source:
            self.run("docker", "exec", "-i", self.args.postgres_container, "pg_restore", "-l", stdin=source)
        self.run("tar", "-C", str(data), "-cf", str(folder / "yjs.tar"), ".")
        for name in ("private-api.env", "private-web.env", "private-yjs.env"):
            source = data.parent / name
            if not source.is_file():
                raise UpgradeError("Required private configuration is missing")
            shutil.copy2(source, folder / name)
            os.chmod(folder / name, 0o600)
        shutil.copy2(self.args.nginx_site, folder / "nginx-site.conf")
        save_json(folder / "data-counts.json", self.data_counts(db))
        available = set(self.run("docker", "ps", "-a", "--format", "{{.Names}}").splitlines())
        selected = self.names if db == self.args.new_db else self.old_names
        save_json(folder / "private-container-runtime.json", {
            kind: self.inspect(name) for kind, name in selected.items() if name in available})
        return folder

    def data_counts(self, db: str) -> dict:
        tables = ("users", "rooms", "trip_understandings", "trip_understanding_revisions", "trip_understanding_sources", "trip_stay_selections")
        fields = ",".join(f"'{table}',(SELECT count(*) FROM {table})" for table in tables)
        return json.loads(self.query(db, f"SELECT json_build_object({fields})"))

    def verify_restored_counts(self, backup: Path, db: str) -> None:
        if self.data_counts(db) != json.loads((backup / "data-counts.json").read_text()):
            raise UpgradeError("Restored business table counts changed; source database is retained")

    def clone(self, backup: Path, db: str, data_name: str) -> Path:
        if self.query("postgres", f"SELECT count(*) FROM pg_database WHERE datname='{identifier(db)}'") != "0":
            raise UpgradeError("Clone database already exists; it will not be overwritten")
        data = self.target / data_name
        if data.exists():
            raise UpgradeError("Clone collaboration directory already exists")
        self.run("docker", "exec", self.args.postgres_container, "createdb", "-U", "postgres", db)
        with (backup / "database.dump").open("rb") as source:
            self.run("docker", "exec", "-i", self.args.postgres_container, "pg_restore", "-U", "postgres", "-d", db, "--exit-on-error", stdin=source)
        self.verify_restored_counts(backup, db)
        data.mkdir(mode=0o700)
        self.run("tar", "-C", str(data), "-xf", str(backup / "yjs.tar"))
        return data

    def configure_api(self, db: str, *, live: bool) -> None:
        if db != (self.args.new_db if live else self.args.rehearsal_db):
            raise UpgradeError("Application configuration must target the selected isolated database")
        values = dict(self.environment)
        values["DATABASE_URL"] = database_url(values["DATABASE_URL"], db)
        redis = urlsplit(values["REDIS_URL"])
        index = self.args.redis_db if live else self.args.preview_redis_db
        values["REDIS_URL"] = urlunsplit((redis.scheme, redis.netloc, "/" + str(index), redis.query, ""))
        values.update(AUTO_MIGRATE="false", REQUIRE_SCHEMA_CHECK="true", CHECKPOINT_BOOTSTRAP_ON_START="false",
                      EXPERIENCE_WORKERS_ENABLED="true", RUNTIME_PROFILE="public", DEMO_MODE="false",
                      AMAP_MOCK="false", TRIP_UNDERSTANDING_PROVIDER_MODE="live",
                      TRIP_UNDERSTANDING_QWEN_DEADLINE_SECONDS=str(self.args.model_deadline_seconds),
                      TRIP_UNDERSTANDING_QWEN_MAX_OUTPUT_TOKENS=str(self.args.model_max_output_tokens))
        if not live:
            values["TRIP_UNDERSTANDING_COOKIE_NAME"] = "bt_upgrade_preview"
            values["CORS_ORIGIN_REGEX"] = rf"^http://127\.0\.0\.1:{self.ports['web']}$"
        required = ("QWEN_API_KEY", "AMAP_API_KEY", "JWT_SECRET_KEY", "TRIP_UNDERSTANDING_COOKIE_SIGNING_KEY", "TRIP_UNDERSTANDING_SOURCE_ENCRYPTION_KEY")
        if any(not values.get(key) for key in required):
            raise UpgradeError("Real preview/live configuration requires existing provider and identity keys")
        self.private_env(self.target / "private-api.env", values)
        self.private_env(self.target / "private-yjs.env", {
            "JWT_SECRET_KEY": values["JWT_SECRET_KEY"], "HOST": "0.0.0.0", "PORT": "1234", "YPERSISTENCE": "/data"})

    def migrate(self) -> None:
        self.check_resources("migrate")
        # The only migration invocation. Runtime containers never use experience_container.py.
        code = "import asyncio,os,sys; sys.path[:0]=['/release/scripts','/release/backend']; from experience import migrate; asyncio.run(migrate({},dsn=os.environ['DATABASE_URL']))"
        self.run("docker", "run", "--rm", "--name", self.target.name + "-migrate",
                 "--label", f"breeze.upgrade.release={self.target.name}", "--network", self.network, "--memory=900m",
                 "--env-file", str(self.target / "private-api.env"), "-v", f"{self.target}/src:/release:ro",
                 "-w", "/release", "--entrypoint", "python", self.containers["api"]["Image"], "-c", code, timeout=300)

    def start(self, source: Path, data: Path, *, live: bool) -> None:
        db = self.args.new_db if live else self.args.rehearsal_db
        self.assert_writer_set(db, set())
        if live:
            if any(self.inspect(n)["State"]["Running"] for n in (self.args.current_api, self.args.current_yjs)):
                raise UpgradeError("Previous and new live writers may not overlap")
        for kind in ("api", "yjs", "web"):
            args = ["docker", "run", "-d", "--name", self.names[kind], "--network", self.network,
                    "--label", f"breeze.upgrade.release={self.target.name}", "--restart", "unless-stopped",
                    "--log-opt", "max-size=10m", "--log-opt", "max-file=3"]
            if kind == "api":
                args += [f"--memory={RUNTIME_MEMORY_MIB[kind]}m", f"--memory-swap={RUNTIME_MEMORY_MIB[kind]}m", "-p", f"127.0.0.1:{self.ports[kind]}:8006", "--env-file", str(self.target / "private-api.env"),
                         "-v", f"{source}/backend:/release/backend:ro", "-w", "/release/backend", "--entrypoint", "python",
                         self.containers[kind]["Image"], "-m", "uvicorn", "app.experience_main:app", "--host", "0.0.0.0", "--port", "8006", "--no-access-log"]
            elif kind == "yjs":
                args += [f"--memory={RUNTIME_MEMORY_MIB[kind]}m", f"--memory-swap={RUNTIME_MEMORY_MIB[kind]}m", "-p", f"127.0.0.1:{self.ports[kind]}:1234", "--env-file", str(self.target / "private-yjs.env"),
                         "-v", f"{source}/y-websocket/server.js:/app/server.js:ro", "-v", f"{data}:/data", "-w", "/app", "--entrypoint", "node",
                         self.containers[kind]["Image"], "server.js"]
            else:
                web_source = source if live else self.target / "preview-src"
                args += [f"--memory={RUNTIME_MEMORY_MIB[kind]}m", f"--memory-swap={RUNTIME_MEMORY_MIB[kind]}m", "-p", f"127.0.0.1:{self.ports[kind]}:3000", "--env-file", str(self.target / ("private-web.env" if live else "private-preview-web.env")),
                         "-e", "HOSTNAME=0.0.0.0", "-e", "PORT=3000",
                         "-v", f"{web_source}/frontend/.next/standalone:/release:ro", "-v", f"{web_source}/frontend/.next/static:/release/.next/static:ro",
                         "-v", f"{web_source}/frontend/public:/release/public:ro", "-w", "/release", "--entrypoint", "node",
                         self.containers[kind]["Image"], "server.js"]
            self.run(*args)
        self.health()

    def health(self, *, ports: dict[str, int] | None = None, context: str = "Private application") -> None:
        for kind, port in (self.ports if ports is None else ports).items():
            deadline = time.monotonic() + 40
            while (remaining := deadline - time.monotonic()) > 0:
                try:
                    with urlopen(f"http://127.0.0.1:{port}" + ("/" if kind == "web" else "/health"), timeout=min(3, remaining)) as response:
                        if response.status == 200:
                            break
                except OSError:
                    pass
                time.sleep(min(1, max(0, deadline - time.monotonic())))
            else:
                raise UpgradeError(f"{context}: {kind} health did not become ready within 40 seconds; data and backups retained")

    def rehearse(self) -> None:
        if self.phase() in {"COPY_RESTORED", "PREVIEW_SERVING_UNVERIFIED"}:
            return
        if self.phase() not in {"PREPARED", "COPY_BACKUP_READY", "COPY_RESTORED_UNMIGRATED"}:
            raise UpgradeError("Rehearsal needs prepared source or a known resumable copy phase; retained partial clones are never overwritten")
        self.check_resources("migrate")
        if self.phase() == "PREPARED":
            self.record("SNAPSHOTTING_REHEARSAL")
            with self.stopped_previous_writers():
                backup = self.backup(self.args.current_db, self.current / "yjs-data", "rehearsal")
                # Keep the completed backup location even if old-service recovery fails.
                self.record("SNAPSHOTTING_REHEARSAL", rehearsal_backup=str(backup))
            self.record("COPY_BACKUP_READY", rehearsal_backup=str(backup))
        backup = Path(self.state["rehearsal_backup"])
        if self.phase() == "COPY_BACKUP_READY":
            self.clone(backup, self.args.rehearsal_db, "rehearsal-yjs-data")
            self.record("COPY_RESTORED_UNMIGRATED", rehearsal_database=self.args.rehearsal_db)
        self.configure_api(self.args.rehearsal_db, live=False)
        self.migrate()
        self.verify_restored_counts(backup, self.args.rehearsal_db)
        self.record("COPY_RESTORED", rehearsal_migrations=self.query(self.args.rehearsal_db, "SELECT filename FROM applied_migrations ORDER BY filename").splitlines())

    def pause_copied_jobs(self) -> None:
        """Only the disposable copy: do not replay production's queued provider calls."""
        db = self.args.rehearsal_db
        if db in {self.args.current_db, self.args.new_db}:
            raise UpgradeError("Copied jobs can only be paused in the preview database")
        self.assert_writer_set(db, set())
        tables = (("trip_understanding_jobs", "RUNNING", "FAILED"),
                  ("trip_map_render_jobs", "BUILDING", "UNAVAILABLE"),
                  ("trip_stay_recommendation_jobs", "BUILDING", "UNAVAILABLE"),
                  ("trip_daily_dining_jobs", "BUILDING", "UNAVAILABLE"))
        statements = [f"UPDATE {table} SET status='{terminal}',lease_owner=NULL,lease_until=NULL,finished_at=NOW() WHERE status IN ('QUEUED','{running}')"
                      for table, running, terminal in tables]
        self.run("docker", "exec", self.args.postgres_container, "psql", "-X", "-q", "-U", "postgres", "-d", db,
                 "-v", "ON_ERROR_STOP=1", "-c", "BEGIN; " + "; ".join(statements) + "; COMMIT;")

    def preview(self) -> None:
        if self.state.get("phase") == "PREVIEW_SERVING_UNVERIFIED":
            self.health()
            return
        if self.phase() not in {"COPY_RESTORED", "PREVIEW_BUILDING", "PREVIEW_STARTING", "PREVIEW_SERVING_UNVERIFIED"}:
            raise UpgradeError("Preview requires a restored copy; health never substitutes for a real user journey")
        self.remove_new_containers()
        if "preview_started_at" not in self.state:
            self.check_resources("build")
            self.record("PREVIEW_BUILDING")
            preview_source = self.target / "preview-src"
            if not preview_source.exists():
                shutil.copytree(self.target / "src/frontend", preview_source / "frontend",
                    ignore=shutil.ignore_patterns("node_modules", ".next", ".next-dev", ".env*"))
            values = dict(line.split("=", 1) for line in (self.target / "private-web.env").read_text().splitlines() if "=" in line)
            values.update(NEXT_PUBLIC_API_URL="", NEXT_PUBLIC_Y_WEBSOCKET_URL=f"ws://127.0.0.1:{self.ports['yjs']}")
            self.private_env(self.target / "private-preview-web.env", values)
            self.build_web(preview_source, preview=True)
        self.check_resources("preview")
        self.configure_api(self.args.rehearsal_db, live=False)
        if "preview_started_at" not in self.state:
            self.assert_redis_unused(self.args.preview_redis_db)
            self.pause_copied_jobs()
            self.record("PREVIEW_STARTING", preview_started_at=datetime.now(timezone.utc).isoformat())
        self.start(self.target / "src", self.target / "rehearsal-yjs-data", live=False)
        self.record("PREVIEW_SERVING_UNVERIFIED")

    def verify_preview_journey(self) -> None:
        resource = self.args.preview_trip_id or ""
        if not re.fullmatch(r"[A-Za-z0-9_-]{20,120}", resource):
            raise UpgradeError("Activation needs --preview-trip-id from a newly created, saved and edited real preview trip")
        started = datetime.fromisoformat(self.state["preview_started_at"]).isoformat()
        report = json.loads(self.query(self.args.rehearsal_db, f"""SELECT COALESCE((SELECT json_build_object(
            'new_in_preview',u.created_at>='{started}'::timestamptz,
            'saved',u.owner_user_id IS NOT NULL,
            'complete',COALESCE((result.public_json->'coverage'->>'complete')::boolean,false),
            'edited',EXISTS(SELECT 1 FROM trip_understanding_revisions r WHERE r.understanding_id=u.understanding_id AND r.proposal_json->>'kind'='USER_EDIT'),
            'model_bindings',COALESCE((SELECT json_agg(r.inference_binding_json) FROM trip_understanding_revisions r
                WHERE r.understanding_id=u.understanding_id),'[]'::json))
            FROM trip_understandings u JOIN trip_understanding_results result ON result.result_id=u.current_result_id
            WHERE u.public_resource_id='{resource}'),'{{}}'::json)"""))
        bindings = report.pop("model_bindings", [])
        real_model = isinstance(bindings, list) and has_live_model_call(bindings, self.environment.get("TRIP_UNDERSTANDING_QWEN_MODEL", ""))
        if set(report) != {"new_in_preview", "saved", "complete", "edited"} or not all(v is True for v in report.values()) or not real_model:
            raise UpgradeError("Preview trip has not completed real model generation, complete readback, account save and a persisted edit")
        # A narrow DB check is a prerequisite, never the full product acceptance.

    def activate(self) -> None:
        if self.phase() in {"LIVE", "RECOVERED_KEEPING_CURRENT_DATA"}:
            self.health()
            return
        if self.state.get("traffic_may_have_switched"):
            raise UpgradeError("Public writes may exist; use recover without reverting the database")
        if self.phase() != "PREVIEW_SERVING_UNVERIFIED":
            raise UpgradeError("Activation requires the real isolated preview plus user-path acceptance")
        self.verify_preview_journey()
        self.assert_redis_unused(self.args.redis_db)
        self.remove_new_containers()
        self.record("SNAPSHOTTING_LIVE")
        with self.stopped_previous_writers():
            backup = self.backup(self.args.current_db, self.current / "yjs-data", "live")
            data = self.clone(backup, self.args.new_db, "yjs-data")
            self.configure_api(self.args.new_db, live=True)
            self.record("MIGRATING_LIVE", live_backup=str(backup))
            self.migrate()
            self.verify_restored_counts(backup, self.args.new_db)
            self.start(self.target / "src", data, live=True)
            config = self.args.nginx_site.read_text()
            updated = replace_upstreams(config, {self.current_ports[k]: self.ports[k] for k in self.ports})
            # Mark BEFORE touching routing: a lost acknowledgement cannot prove no new writes.
            self.record("SWITCHING_TRAFFIC", traffic_may_have_switched=True)
            self.args.nginx_site.write_text(updated)
            self.run("nginx", "-t")
            self.run("systemctl", "reload", "nginx")
            self.health()
            # App dependencies must return after host restart; stopped legacy writers remain stopped.
            self.run("docker", "update", "--restart", "unless-stopped", self.args.postgres_container, self.args.redis_container)
            self.record("LIVE", live_source=str(self.target / "src"))

    def recover(self) -> None:
        if not self.state.get("traffic_may_have_switched"):
            raise UpgradeError("No possible public cutover recorded; previous release remains the recovery target")
        if self.state.get("phase") == "RECOVERED_KEEPING_CURRENT_DATA":
            self.health()
            return
        # The deployed first-release StrictModel rejects actual new public_json
        # fields. Reusing old code would make saved trips unreadable.
        source = self.target / "src"
        self.remove_new_containers(remove=False)
        self.assert_writer_set(self.args.new_db, set())
        backup = self.backup(self.args.new_db, self.target / "yjs-data", "recovery")
        self.record("RECOVERING_WITH_CURRENT_DATA", recovery_backup=str(backup))
        self.remove_new_containers()
        self.configure_api(self.args.new_db, live=True)
        self.start(source, self.target / "yjs-data", live=True)
        saved = Path(self.state["live_backup"]) / "nginx-site.conf"
        updated = replace_upstreams(saved.read_text(), {self.current_ports[k]: self.ports[k] for k in self.ports})
        if self.args.nginx_site.read_text() not in {saved.read_text(), updated}:
            raise UpgradeError("Nginx changed beyond the release ports; preserve it and inspect")
        self.args.nginx_site.write_text(updated)
        self.run("nginx", "-t")
        self.run("systemctl", "reload", "nginx")
        self.run("docker", "update", "--restart", "unless-stopped", self.args.postgres_container, self.args.redis_container)
        self.record("RECOVERED_KEEPING_CURRENT_DATA", live_source=str(source))

    def execute(self) -> None:
        if not self.args.execute:
            report = self.snapshot()
            self.check_source_ref()
            report.update(action=self.args.action, will_mutate=False,
                          operations={"prepare": ["extract new source", "build frontend"],
                                      "rehearse": ["briefly stop old writers", "backup current data and configuration", "resume old writers", "restore isolated rehearsal database and Yjs", "explicit migration", "COPY_RESTORED means no application or user-path acceptance"],
                                      "preview": ["build separate preview frontend with isolated API/Yjs addresses", "pause inherited jobs only in the copy", "real provider keys and workers against copy DB and separate Redis", "PREVIEW_SERVING_UNVERIFIED: use SSH forwards and execute real user paths"],
                                      "activate": ["read newly generated/saved/edited preview trip; broader acceptance remains separate", "stop private preview", "stop old writers", "fresh backup and separate live clone", "explicit migration", "private health", "switch nginx", "enable dependency restart"],
                                      "recover": ["stop new writers", "back up current live data", "current release code against current data", "keep new database and Yjs; old code is incompatible"]}[self.args.action])
            print(json.dumps(report, indent=2))
            return
        if platform.system() != "Linux" or os.geteuid() != 0:
            raise UpgradeError("Execution requires the authorized Linux host administrator")
        import fcntl
        os.umask(0o077)
        with (self.current.parent / ".breezetravel-upgrade.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise UpgradeError("Another application upgrade is in progress") from None
            self.snapshot()
            self.check_source_ref()
            try:
                getattr(self, self.args.action)()
            except Exception as original:
                failures = []
                if self.mutation_started and self.state and not self.state.get("traffic_may_have_switched"):
                    # Discard only disposable containers; never delete databases or collaboration files.
                    try:
                        self.remove_new_containers()
                    except Exception as exc:
                        failures.append(("final container cleanup", exc))
                if self.mutation_started and self.state_path.exists():
                    try:
                        self.record("FAILED_" + (self.phase() or "UNKNOWN"))
                    except Exception as exc:
                        failures.append(("failure state update", exc))
                if failures:
                    messages = [self.failure_detail(original)]
                    messages.extend(f"{stage}: {self.failure_detail(error)}" for stage, error in failures)
                    raise UpgradeError("; ".join(messages) + "; data and backups retained") from original
                raise


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "rehearse", "preview", "activate", "recover"))
    parser.add_argument("--execute", action="store_true", help="Explicitly perform mutations; omitted means read-only plan")
    for name in ("expected-host", "current-release", "current-db", "current-api", "current-web", "current-yjs", "new-release", "new-db", "rehearsal-db"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--postgres-container", default="breezetravel-postgres-1")
    parser.add_argument("--redis-container", default="breezetravel-redis-1")
    parser.add_argument("--nginx-site", type=Path, default=Path("/etc/nginx/sites-available/breezetravel.cn"))
    parser.add_argument("--api-port", type=int, required=True)
    parser.add_argument("--web-port", type=int, required=True)
    parser.add_argument("--yjs-port", type=int, required=True)
    parser.add_argument("--redis-db", type=int, required=True)
    parser.add_argument("--preview-redis-db", type=int, required=True)
    parser.add_argument("--preview-trip-id", help="Actual newly generated, account-saved and edited preview trip; required by activate")
    parser.add_argument("--model-deadline-seconds", type=float, required=True)
    parser.add_argument("--model-max-output-tokens", type=int, required=True)
    parser.add_argument("--source-archive", type=Path)
    parser.add_argument("--source-ref", default="unspecified", help="Ordinary Git ref for identifying the deployed code")
    return parser.parse_args(argv)


if __name__ == "__main__":
    try:
        Upgrade(parse_args()).execute()
    except UpgradeError as exc:
        raise SystemExit(str(exc) + "; data and the saved phase were retained.") from None
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired):
        raise SystemExit("Upgrade did not complete; inspect the saved phase. Private errors were suppressed; data was not deleted.") from None
