#!/usr/bin/env bash
set -euo pipefail

# This runner is a test-only, credential-free execution envelope. It starts
# loopback services only for checks whose frozen argv explicitly requires them.
# Docker supplies no external network, host mount, host PID namespace, Provider
# secret, authority key, or user profile to this container.

readonly command_text=" $* "
needs_postgres=false
needs_browser_stack=false

case "${command_text}" in
  *"test_agent_gate_live_registry_postgres.py"*|*"test_g01_map_positive_postgres.py"*|*"test_trip_understanding_v3_postgres.py"*)
    needs_postgres=true
    ;;
esac
case "${command_text}" in
  *"test:e2e:g01"*)
    needs_postgres=true
    needs_browser_stack=true
    ;;
esac

child_pids=()
cleanup() {
  if ((${#child_pids[@]})); then
    kill "${child_pids[@]}" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

wait_for_command() {
  local attempts="$1"
  shift
  for ((attempt = 1; attempt <= attempts; attempt++)); do
    if "$@" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  return 1
}

start_postgres() {
  export PGDATA=/tmp/breezetravel-agent-gate-pgdata
  rm -rf "${PGDATA}"
  mkdir -p "${PGDATA}"
  initdb -D "${PGDATA}" --auth-local=trust --auth-host=trust >/tmp/initdb.log
  postgres -D "${PGDATA}" -h 127.0.0.1 -p 5432 >/tmp/postgres.log 2>&1 &
  child_pids+=("$!")
  wait_for_command 60 pg_isready -h 127.0.0.1 -p 5432 -U postgres
  createdb -h 127.0.0.1 -p 5432 -U postgres travel_agent

  export RUN_SERVICE_INTEGRATION=1
  export TEST_DATABASE_ADMIN_URL=postgresql://postgres@127.0.0.1:5432/postgres
  export DATABASE_URL=postgresql+asyncpg://postgres@127.0.0.1:5432/travel_agent
}

start_browser_stack() {
  export REDIS_URL=redis://127.0.0.1:6379
  export RUNTIME_PROFILE=local_fixture
  export DEMO_MODE=false
  export AMAP_MOCK=true
  export DEV_LOGIN_BYPASS=true
  export AUTO_MIGRATE=false
  export REQUIRE_SCHEMA_CHECK=true
  export JWT_SECRET_KEY=breezetravel-agent-gate-test-only-jwt-key
  export TRIP_UNDERSTANDING_SOURCE_ENCRYPTION_KEY=breezetravel-agent-gate-test-only-source-key
  export BACKEND_INTERNAL_URL=http://127.0.0.1:8000
  export E2E_BASE_URL=http://127.0.0.1:3000

  redis-server \
    --bind 127.0.0.1 \
    --port 6379 \
    --save '' \
    --appendonly no \
    --dir /tmp >/tmp/redis.log 2>&1 &
  child_pids+=("$!")
  wait_for_command 60 redis-cli -h 127.0.0.1 ping

  (
    cd /workspace/backend
    python -m scripts.migrate
  ) >/tmp/migrate.log 2>&1
  (
    cd /workspace/backend
    python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
  ) >/tmp/backend.log 2>&1 &
  child_pids+=("$!")
  (
    cd /workspace/backend
    python -m app.trip_understanding.worker
  ) >/tmp/trip-understanding-worker.log 2>&1 &
  child_pids+=("$!")
  (
    cd /workspace/backend
    python -m app.trip_understanding.map_worker
  ) >/tmp/map-render-worker.log 2>&1 &
  child_pids+=("$!")
  wait_for_command 120 curl --fail --silent http://127.0.0.1:8000/health

  (
    cd /workspace/frontend
    npm run build
    PORT=3000 HOSTNAME=127.0.0.1 npm run start
  ) >/tmp/frontend.log 2>&1 &
  child_pids+=("$!")
  wait_for_command 300 curl --fail --silent http://127.0.0.1:3000/
}

if [[ "${needs_postgres}" == true ]]; then
  start_postgres
fi
if [[ "${needs_browser_stack}" == true ]]; then
  start_browser_stack
fi

"$@"
