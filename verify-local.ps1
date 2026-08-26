param(
    [switch]$SkipInstall,
    [switch]$SkipBrowser,
    [switch]$WithServices
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$summaryDir = Join-Path $root "backend\evidence\verification"
New-Item -ItemType Directory -Force -Path $summaryDir | Out-Null
$records = [System.Collections.Generic.List[object]]::new()

# The RC1 verification entrypoint is deliberately offline.  Explicitly mask
# inherited provider and tracing credentials so a developer shell cannot turn
# an offline gate into a paid call or an observability upload.
$offlineEnvironment = @{
    DEEPSEEK_API_KEY = ""
    OPENAI_API_KEY = ""
    AMAP_API_KEY = ""
    LANGCHAIN_TRACING_V2 = "false"
    LANGSMITH_TRACING = "false"
}
$savedEnvironment = @{}
foreach ($name in $offlineEnvironment.Keys) {
    $savedEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, "Process")
    [Environment]::SetEnvironmentVariable($name, $offlineEnvironment[$name], "Process")
}

function Invoke-Check {
    param([string]$Name, [string]$WorkingDirectory, [scriptblock]$Command)
    $started = Get-Date
    Push-Location $WorkingDirectory
    try {
        & $Command
        $code = $LASTEXITCODE
        if ($null -eq $code) { $code = 0 }
        if ($code -ne 0) { throw "$Name failed with exit code $code" }
        $records.Add([ordered]@{ name=$Name; status="passed"; duration_seconds=[math]::Round(((Get-Date)-$started).TotalSeconds,3) })
    } catch {
        $records.Add([ordered]@{ name=$Name; status="failed"; duration_seconds=[math]::Round(((Get-Date)-$started).TotalSeconds,3); error=$_.Exception.Message })
        throw
    } finally {
        Pop-Location
    }
}

if (-not $SkipInstall) {
    Invoke-Check "backend-install" "$root\backend" { python -m pip install -r requirements-dev.txt }
    Invoke-Check "frontend-install" "$root\frontend" { npm ci }
    Invoke-Check "yjs-install" "$root\y-websocket" { npm ci }
}

Invoke-Check "backend-ruff" "$root\backend" { python -m ruff check app evals scripts tests }
foreach ($seed in 17, 42, 91) {
    Invoke-Check "backend-pytest-seed-$seed" "$root\backend" { python -m pytest -m "not external and not integration and not local_e2e" --randomly-seed=$seed --tb=short }
}
if ($WithServices) {
    $env:RUN_SERVICE_INTEGRATION = "1"
    try {
        Invoke-Check "backend-service-integration" "$root\backend" { python -m pytest -m integration --tb=short }
    } finally {
        Remove-Item Env:RUN_SERVICE_INTEGRATION -ErrorAction SilentlyContinue
    }
}
Invoke-Check "backend-local-eval" "$root\backend" { python -m scripts.run_local_eval_suite }
Invoke-Check "backend-fault-eval" "$root\backend" { python -m scripts.run_fault_injection }
Invoke-Check "backend-experiments" "$root\backend" { python -m scripts.run_local_experiments }
Invoke-Check "frontend-typecheck" "$root\frontend" { npx tsc --noEmit }
Invoke-Check "frontend-build" "$root\frontend" { npm run build }
Invoke-Check "yjs-tests" "$root\y-websocket" { npm test }
Invoke-Check "compose-config" $root { docker compose config --quiet }
Invoke-Check "compose-multi-config" $root { docker compose -f docker-compose.multi.yml config --quiet }
if (-not $SkipBrowser) {
    Invoke-Check "local-browser-e2e" "$root\frontend" { npm run test:e2e:local }
    if ($WithServices) {
        Invoke-Check "chat-persistence-e2e" "$root\frontend" { npm run test:e2e:persistence }
    }
}
Invoke-Check "git-diff-check" $root { git diff --check }

$summary = [ordered]@{
    generated_at = (Get-Date).ToUniversalTime().ToString("o")
    environment = "local-controlled"
    public_deployment_tested = $false
    real_users_tested = $false
    records = $records
}
$summary | ConvertTo-Json -Depth 8 | Set-Content -Encoding utf8 "$summaryDir\summary.json"
Write-Host "Local verification completed: $summaryDir\summary.json"

foreach ($name in $offlineEnvironment.Keys) {
    [Environment]::SetEnvironmentVariable($name, $savedEnvironment[$name], "Process")
}
