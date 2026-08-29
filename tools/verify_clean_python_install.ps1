param(
    [string]$PythonExecutable = "python",
    [string]$VenvPath = "runtime\ci-venv",
    [switch]$RunFullTests
)

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

& $PythonExecutable -m venv $resolvedVenv
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$python = Join-Path $resolvedVenv "Scripts\python.exe"
& $python -m pip install --upgrade "pip==26.2" "setuptools==83.0.0" "wheel==0.47.0"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $python -m pip install -r (Join-Path $repoRoot "requirements-dev.txt")
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $python -m pip install --no-deps --no-build-isolation -e $repoRoot
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$env:AMADEUS_E2E_NO_TTS = "1"
$env:TTS_DEVICE = "cpu"
$env:WORK_WORKTREE_ISOLATION = "0"
$env:AUIP_APPSESSION_ROLE_BRANCH_MODE = "off"
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
