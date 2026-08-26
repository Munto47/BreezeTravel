$ErrorActionPreference = 'Stop'

$frontendRoot = Split-Path -Parent $PSScriptRoot
$repositoryRoot = Split-Path -Parent $frontendRoot
$diagnosticPath = Join-Path $repositoryRoot 'backend\evidence\runtime_diagnostics\backend_yjs_restart_latest.json'
$services = @('postgres', 'redis', 'backend', 'y-websocket')
$initiallyRunning = @{}
$startedByRunner = @()
$diagnosticWritten = $false

function Write-DiagnosticReceipt {
    param(
        [string]$Status,
        [string]$ReasonCode,
        [string]$Message
    )
    $directory = Split-Path -Parent $diagnosticPath
    New-Item -ItemType Directory -Path $directory -Force | Out-Null
    $receipt = [ordered]@{
        schema_version = 'backend-yjs-restart-runtime-diagnostic-v1'
        status = $Status
        decision = if ($Status -eq 'PASS') { 'ACCEPT_LOCAL_FIXTURE_GATE' } else { 'REJECT' }
        reason_code = $ReasonCode
        message = $Message
        generated_at = [DateTimeOffset]::UtcNow.ToString('o')
        claim_scope = 'local_fixture_backend_yjs_restart_only'
        docker_required = $true
        sqlite_substitute_used = $false
        service_targets = @('backend', 'y-websocket')
        required_independent_cases = 9
        matrix_version = 'g5-backend-yjs-restart-v1'
        initial_running = $initiallyRunning
        services_started_by_runner = $startedByRunner
    }
    $json = $receipt | ConvertTo-Json -Depth 8
    [System.IO.File]::WriteAllText($diagnosticPath, "$json`n", [System.Text.UTF8Encoding]::new($false))
    $script:diagnosticWritten = $true
}

Push-Location $repositoryRoot
try {
    try {
        docker info --format '{{json .ServerVersion}}' | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "docker info exited with code $LASTEXITCODE" }
    } catch {
        Write-DiagnosticReceipt -Status 'UNAVAILABLE' -ReasonCode 'DOCKER_ENGINE_UNAVAILABLE' -Message $_.Exception.Message
        throw
    }

    foreach ($service in $services) {
        $runningId = docker compose ps -q --status running $service
        if ($LASTEXITCODE -ne 0) { throw "docker compose ps failed for $service" }
        $initiallyRunning[$service] = -not [string]::IsNullOrWhiteSpace(($runningId | Out-String).Trim())
    }
    # One-run cleanup authority is injected only into the two named local
    # services and inherited by Playwright. It is never written to evidence.
    $env:E2E_CLEANUP_SECRET = [Guid]::NewGuid().ToString('N')
    $env:E2E_RESTART_GATE_MODE = 'true'
    # Backend source is bind-mounted by Compose. Yjs server.js is copied into
    # its image, so rebuild that one named service to prevent a stale health
    # contract from being mistaken for a port collision.
    docker compose up -d postgres redis migrate backend
    if ($LASTEXITCODE -ne 0) { throw "docker compose up exited with code $LASTEXITCODE" }
    docker compose up -d --build y-websocket
    if ($LASTEXITCODE -ne 0) { throw "docker compose up exited with code $LASTEXITCODE" }
    $startedByRunner = @($services | Where-Object { -not $initiallyRunning[$_] })

    $deadline = (Get-Date).AddMinutes(2)
    do {
        try {
            $health = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:8000/health' -TimeoutSec 2
            $yjs = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:1234' -TimeoutSec 2
            $healthBody = $health.Content | ConvertFrom-Json
            $yjsBody = $yjs.Content | ConvertFrom-Json
            if (
                $health.StatusCode -eq 200 -and
                $yjs.StatusCode -eq 200 -and
                $healthBody.service -eq 'breezetravel-backend' -and
                $yjsBody.service -eq 'breezetravel-yjs' -and
                $healthBody.boot_generation.instance_id -and
                $yjsBody.boot_generation.instance_id
            ) { break }
        } catch {
            Start-Sleep -Milliseconds 300
        }
    } while ((Get-Date) -lt $deadline)

    if ((Get-Date) -ge $deadline) {
        throw 'Backend or Yjs did not become ready within two minutes.'
    }

    $backendMock = docker compose exec -T backend printenv AMAP_MOCK
    $ftRouter = docker compose exec -T backend printenv FT_ROUTER_ENABLED
    if ($backendMock.Trim() -ne 'true') { throw 'Refusing E2E: backend AMAP_MOCK must be true.' }
    if ($ftRouter.Trim() -ne 'false') { throw 'Refusing E2E: FT_ROUTER_ENABLED must be false.' }

    Push-Location $frontendRoot
    try {
        $env:BREEZE_E2E_ALLOW_SERVICE_RESTART = '1'
        npx playwright test -c playwright.dual-user-restart.config.js
        if ($LASTEXITCODE -ne 0) { throw "Playwright exited with code $LASTEXITCODE" }
        Write-DiagnosticReceipt -Status 'PASS' -ReasonCode 'LOCAL_FIXTURE_NINE_CASE_RESTART_GATE_PASSED' -Message 'Nine isolated cases completed with one real process replacement and exact public HTTP/Yjs/browser readback.'
    } finally {
        Remove-Item Env:\BREEZE_E2E_ALLOW_SERVICE_RESTART -ErrorAction SilentlyContinue
        Pop-Location
    }
} catch {
    if (-not $diagnosticWritten) {
        Write-DiagnosticReceipt -Status 'FAILED' -ReasonCode 'BACKEND_YJS_RESTART_GATE_FAILED' -Message $_.Exception.Message
    }
    throw
} finally {
    if ($startedByRunner.Count -gt 0) {
        # Restore only named Compose services that this runner itself started.
        # Never enumerate PIDs and never stop a pre-existing user service.
        docker compose stop @startedByRunner | Out-Null
    }
    Remove-Item Env:\E2E_CLEANUP_SECRET -ErrorAction SilentlyContinue
    Remove-Item Env:\E2E_RESTART_GATE_MODE -ErrorAction SilentlyContinue
    Pop-Location
}
