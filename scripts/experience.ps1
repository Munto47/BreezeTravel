param(
    [ValidateSet('start','stop','restart','status','backup','restore','migrate','configure')]
    [string]$Action = 'start',
    [switch]$NoWeb,
    [switch]$Dev,
    [string]$BackupFile
)
$ErrorActionPreference = 'Stop'
$ExperienceRoot = Split-Path -Parent $PSScriptRoot
$ExperiencePython = $env:EXPERIENCE_PYTHON
if (-not $ExperiencePython) {
    $ExperienceCandidates = @(
        (Join-Path $ExperienceRoot '.venv/Scripts/python.exe'),
        (Join-Path (Split-Path -Parent $ExperienceRoot) 'BreezeTravel/.venv/Scripts/python.exe')
    )
    $ExperiencePython = $ExperienceCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
}
if (-not $ExperiencePython) { throw 'Set EXPERIENCE_PYTHON to the installed backend Python runtime.' }
$ExperienceArguments = @((Join-Path $PSScriptRoot 'experience.py'), $Action)
if ($NoWeb) { $ExperienceArguments += '--no-web' }
if ($Dev) { $ExperienceArguments += '--dev' }
if ($BackupFile) { $ExperienceArguments += @('--backup-file', $BackupFile) }
& $ExperiencePython @ExperienceArguments
exit $LASTEXITCODE
