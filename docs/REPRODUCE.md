# Reproducing the controlled-local release

Target runtime is Python 3.11 and Node 20. Dependency versions are pinned in
`backend/requirements.txt`, `backend/requirements-dev.txt` and both npm lockfiles.

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

Public deployment, external API/GPU variants and real-user task validation are
separate gates and are intentionally excluded from this controlled-local release.
