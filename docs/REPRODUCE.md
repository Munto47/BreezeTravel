# Reproducing the controlled-local release

Target runtime is Python 3.11 and Node 20. Dependency versions are pinned in
`backend/requirements.txt`, `backend/requirements-dev.txt` and both npm lockfiles.

The default Compose stack uses `RUNTIME_PROFILE=local_fixture` with
`AMAP_MOCK=true`, so the text-import journey is executable without spending a
Provider quota. Those candidates are explicitly marked `fixture` in the UI;
they are not real POI evidence. Keep `local_real` with `AMAP_MOCK=false` for
the separately authorised live-Provider gate.

## Static and deterministic gates

```powershell
cd backend
python -m pip install -r requirements.txt -r requirements-dev.txt
python -m ruff check app evals scripts tests
python -m pytest -m "not external and not integration and not local_e2e" --randomly-seed=17
python -m pytest -m "not external and not integration and not local_e2e" --randomly-seed=42
python -m pytest -m "not external and not integration and not local_e2e" --randomly-seed=91
python -m scripts.run_local_eval_suite
python -m scripts.run_local_experiments
python -m scripts.run_fault_injection
```

```powershell
cd frontend
npm ci
npm run typecheck
npm run build
npx playwright test --config=playwright.local.config.js

cd ..\y-websocket
npm ci
npm test
```

## PostgreSQL and Redis integration

```powershell
docker compose up -d postgres redis
cd backend
$env:RUN_SERVICE_INTEGRATION="1"
python -m pytest -m integration
```

The migration job is explicit:

```powershell
python -m scripts.migrate
```

## Two-instance controlled-local validation

```powershell
docker compose -f docker-compose.multi.yml up -d postgres redis migrate backend-a backend-b
cd backend
python -m scripts.validate_multi_instance
```

If the pinned registry tags are temporarily unavailable, image overrides are
supported (`POSTGRES_IMAGE`, `REDIS_IMAGE`, `BACKEND_IMAGE`). Any override belongs
in the evidence boundary and must not be described as pinned-image verification.

Finally generate the manifest:

```powershell
python -m scripts.build_release_manifest
```

## Dual-entry local delivery gate

The dual-entry workbench has a separate local delivery record. It uses the
three GPT-5.6-sol `synthetic_proxy` role artifacts only as development
evidence; it neither executes nor substitutes human calibration, live-provider
proof, or public deployment.

```powershell
cd backend
python -m pytest tests -q
python -m ruff check app evals scripts tests
python -m scripts.run_m1_dev_proxy_gate `
  --artifact results/auditor_simulated/proxy_role_1.json `
  --artifact results/auditor_simulated/proxy_role_2.json `
  --artifact results/auditor_simulated/proxy_role_3.json

cd ..\frontend
npm run build
npx playwright test -c playwright.local.config.js
npx playwright test -c playwright.workspace.config.js

cd ..
docker compose up -d postgres
cd backend
$env:RUN_SERVICE_INTEGRATION="1"
python -m pytest tests/test_migrations_integration.py `
  tests/test_templates_sharing_postgres.py `
  tests/test_dual_entry_postgres_integration.py -q
cd ..
docker compose stop postgres
```

Use `python -m scripts.build_release_manifest` afterward. The generated
manifest binds the final plan, capability status, local acceptance record and
M1-dev proxy-gate report by SHA-256, and remains a local delivery candidate.

Public deployment, external API/GPU variants and real-user task validation are
separate gates and are intentionally excluded from this controlled-local release.
