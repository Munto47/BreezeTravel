"""Run tests against this worktree's private PostgreSQL without printing secrets."""
from __future__ import annotations
import subprocess
import sys

import experience


def main():
    values = experience.read_env(experience.ENV_FILE)
    if not values:
        raise SystemExit("Configure this worktree's isolated experience first.")
    env = experience.environment(values)
    dsn = env["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
    env.update(RUN_SERVICE_INTEGRATION="1", TEST_DATABASE_ADMIN_URL=dsn.rsplit("/", 1)[0]+"/postgres",
               TEST_DATABASE_URL=dsn, DATABASE_ADMIN_URL=dsn.rsplit("/",1)[0]+"/postgres",
               AMAP_MOCK="true", TRIP_UNDERSTANDING_PROVIDER_MODE="fixture",
               LANGCHAIN_TRACING_V2="false", LANGSMITH_TRACING="false")
    env["PYTHONIOENCODING"] = "utf-8"
    runner = ["scripts.run_product_checks", *sys.argv[2:]] if sys.argv[1:2] == ["--suite"] else ["pytest", *sys.argv[1:]]
    result = subprocess.run([sys.executable,"-m",*runner],
        cwd=experience.ROOT/"backend", env=env, capture_output=True, text=True, encoding="utf-8")
    output = result.stdout + result.stderr
    for key, value in values.items():
        if value and any(word in key for word in ("KEY","PASSWORD","SECRET","TOKEN")):
            output = output.replace(value, "[REDACTED]")
    target = experience.ROOT/".local-artifacts"/"verification"
    target.mkdir(parents=True, exist_ok=True)
    (target/"latest-tests.txt").write_text(output, encoding="utf-8")
    print(output)
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
