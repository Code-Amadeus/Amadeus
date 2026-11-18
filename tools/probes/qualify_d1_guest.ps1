param([ValidateSet("Install","Synthesize")][string]$Phase)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUNBUFFERED = '1'
$env:UV_CACHE_DIR = 'C:\work\uv-cache'
$env:UV_PYTHON_INSTALL_DIR = 'C:\work\managed-python'
$env:UV_HTTP_TIMEOUT = '100'
$env:UV_LINK_MODE = 'copy'
$env:PATH = 'C:\inputs\bin;' + $env:PATH
$env:TTS_DEVICE = 'cpu'
$env:AMADEUS_E2E_NO_TTS = '0'
$env:WORK_WORKTREE_ISOLATION = '0'
$env:HF_HUB_DISABLE_TELEMETRY = '1'

function Assert-NoBuildTools {
    $tools = @(Get-Command cl.exe,nmake.exe,msbuild.exe,nvcc.exe,gcc.exe,clang.exe -ErrorAction SilentlyContinue)
    $roots = @('C:\BuildTools', 'C:\Program Files\Microsoft Visual Studio', 'C:\Program Files (x86)\Microsoft Visual Studio')
    $keys = @('HKLM:\SOFTWARE\Microsoft\VisualStudio\Setup\Instances', 'HKLM:\SOFTWARE\WOW6432Node\Microsoft\VisualStudio\Setup\Instances')
    $found = @($roots | Where-Object { Test-Path -LiteralPath $_ })
    foreach ($key in $keys) { if (Test-Path $key) { $found += @(Get-ChildItem $key) } }
    $compilers = @(Get-ChildItem -LiteralPath 'C:\Program Files','C:\Program Files (x86)' -Filter cl.exe -Recurse -File -ErrorAction SilentlyContinue)
    if ($tools.Count -or $found.Count -or $compilers.Count) { throw 'Unexpected native development tool or Visual Studio installation' }
    Write-Host "D1_BUILD_TOOLS absent; cl/nmake/MSBuild/nvcc/GCC/Clang not on PATH; Visual Studio paths/instances and host compiler directories clear"
}
function Confirm-Exit([string]$step) {
    if ($LASTEXITCODE -ne 0) { throw "$step failed with exit code $LASTEXITCODE" }
}
Assert-NoBuildTools
if ($Phase -eq 'Install') {
    if (@(Get-ChildItem -LiteralPath 'C:\work' -Force).Count -ne 0) { throw 'Cold scratch directory is not empty' }
    (Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion') |
        Select-Object ProductName,CurrentBuild,InstallationType | Format-List
    New-Item -ItemType Directory -Path 'C:\work\source' | Out-Null
    Set-Location C:\work\source
    tar.exe -xf C:\inputs\source.tar
    Confirm-Exit 'extract main'
    foreach ($path in @('.venv',$env:UV_CACHE_DIR,$env:UV_PYTHON_INSTALL_DIR,'C:\Users\ContainerAdministrator\.cache\uv')) {
        if (Test-Path -LiteralPath $path) { throw "Unexpected prepopulated environment or cache at $path" }
    }
    uv.exe --version
    Confirm-Exit 'uv bootstrap'
    uv.exe python install 3.12.10
    Confirm-Exit 'install managed Python'
    uv.exe venv --python 3.12.10 --managed-python .venv
    Confirm-Exit 'fresh Python environment'
    $clock = [Diagnostics.Stopwatch]::StartNew()
    uv.exe sync --locked --extra local-cpu
    Confirm-Exit 'cold locked T4 installation'
    Write-Host ("D1_COLD_SYNC_SECONDS {0:F1}" -f $clock.Elapsed.TotalSeconds)
    foreach ($name in 'MSVCP140.dll','MSVCP140_1.dll','VCRUNTIME140_1.dll','dxgi.dll') {
        Write-Host "D1_BASE_SYSTEM_DLL $name present=$(Test-Path -LiteralPath (Join-Path $env:SystemRoot ('System32\' + $name)))"
    }
    $redist = Start-Process -FilePath 'C:\inputs\VC_redist.x64.exe' -ArgumentList '/install','/quiet','/norestart' -WindowStyle Hidden -Wait -PassThru
    if ($redist.ExitCode -notin @(0,3010)) { throw "Microsoft VC++ 14.52 x64 redistributable exited $($redist.ExitCode)" }
    Write-Host 'D1_VC_RUNTIME Installed Microsoft Visual C++ Redistributable 14.52.37029.0'
    .\.venv\Scripts\python.exe -c "import onnxruntime; print('D1_ONNXRUNTIME',onnxruntime.__version__)"
    Confirm-Exit 'ONNX Runtime after installing its documented Windows redistributable'
    .\.venv\Scripts\python.exe tools\verify_python_environment.py --profile t4-cpu
    Confirm-Exit 'T4 CPU verifier'
    Assert-NoBuildTools
} else {
    Set-Location C:\work\source
    $python = 'C:\work\source\.venv\Scripts\python.exe'
    &$python tools\stage_japanese_dictionary.py --archive C:\inputs\dictionary.zip --output-root C:\work\source
    Confirm-Exit 'stage pinned Japanese dictionary'
    &$python tools\external_assets.py install C:\inputs\voice.zip
    Confirm-Exit 'verify and install official voice pack'
    &$python -c "import pyopenjtalk; print('D1_OPENJTALK',pyopenjtalk.g2p('\u6e96\u5099\u5b8c\u4e86'))"
    Confirm-Exit 'Japanese frontend'
    $run = Start-Process -FilePath $python -ArgumentList @('-u','C:\inputs\synthesize.py','C:\work') -WindowStyle Hidden -Wait -PassThru -RedirectStandardOutput 'C:\work\synthesis-out.log' -RedirectStandardError 'C:\work\synthesis-err.log'
    if ($run.ExitCode -ne 0) {
        Get-Content -LiteralPath 'C:\work\synthesis-err.log' | Select-String -Pattern '^\w+(Error|Exception):' | Select-Object -Last 4
        throw "Real Japanese synthesis failed with exit code $($run.ExitCode)"
    }
    Get-Content -LiteralPath 'C:\work\synthesis-out.log' | Select-String -Pattern '^D1_SYNTHESIS '
    Assert-NoBuildTools
}
