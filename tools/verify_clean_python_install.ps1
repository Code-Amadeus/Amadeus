param(
    [string]$UvExecutable = "uv",
    [string]$VenvPath = "runtime\ci-venv",
    [switch]$RunFullTests
)

# Clean-install verification for the L1 + dev tooling profile. Creates a
# fresh venv below runtime/ and installs it from the single dependency
# declaration (pyproject.toml) via its uv.lock, exactly as CI does.
$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$resolvedVenv = [System.IO.Path]::GetFullPath((Join-Path $repoRoot $VenvPath))
$runtimeRoot = [System.IO.Path]::GetFullPath((Join-Path $repoRoot "runtime"))

if (-not $resolvedVenv.StartsWith($runtimeRoot + [System.IO.Path]::DirectorySeparatorChar)) {
    throw "VenvPath must resolve below $runtimeRoot"
}
if (Test-Path -LiteralPath $resolvedVenv) {
    throw "Refusing to replace existing environment: $resolvedVenv"
}

& $UvExecutable venv $resolvedVenv --python 3.12
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

# Point uv's project commands at the fresh venv instead of the repo .venv.
$previousProjectEnvironment = $env:UV_PROJECT_ENVIRONMENT
$env:UV_PROJECT_ENVIRONMENT = $resolvedVenv
Push-Location $repoRoot
try {
    & $UvExecutable sync --locked --extra dev
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
} finally {
    Pop-Location
    [Environment]::SetEnvironmentVariable("UV_PROJECT_ENVIRONMENT", $previousProjectEnvironment, "Process")
}

$python = Join-Path $resolvedVenv "Scripts\python.exe"
$env:AMADEUS_E2E_NO_TTS = "1"
$env:TTS_DEVICE = "cpu"
$env:WORK_WORKTREE_ISOLATION = "0"
$env:AUIP_ACTION_PROVIDER = ""
$env:AUIP_ACTION_MODEL = ""
$env:AUIP_ACTION_REASONING_EFFORT = "none"
& $python (Join-Path $repoRoot "tools\verify_python_environment.py") --profile ci
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

if ($RunFullTests) {
    & $python -X utf8 (Join-Path $repoRoot "tools\run_tests.py")
} else {
    & $python -m pytest -q `
        (Join-Path $repoRoot "tests\test_gemini_client.py") `
        (Join-Path $repoRoot "tests\test_system_settings.py")
}
exit $LASTEXITCODE
