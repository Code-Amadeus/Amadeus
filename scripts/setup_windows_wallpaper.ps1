param([switch]$BuildOnly, [switch]$Rebuild)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$helperProject = Join-Path $projectRoot 'wallpaper\windows\Amadeus.Wallpaper\Amadeus.Wallpaper.csproj'
$helperDirectory = Join-Path $projectRoot 'build\windows-wallpaper\host'
$helper = Join-Path $helperDirectory 'Amadeus.Wallpaper.exe'
if (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'Amadeus.Wallpaper.exe')) {
    $helperDirectory = $PSScriptRoot
    $helper = Join-Path $helperDirectory 'Amadeus.Wallpaper.exe'
}
if ($env:OS -ne 'Windows_NT') { throw 'This setup is Windows-only.' }
if ($Rebuild -or !(Test-Path -LiteralPath $helper)) {
    & dotnet publish $helperProject -c Release -r win-x64 --self-contained true -o $helperDirectory --nologo
    if ($LASTEXITCODE -ne 0) { throw 'Windows wallpaper helper build failed.' }
}
if ($BuildOnly) { exit 0 }
$inspection = (& $helper inspect | Select-Object -Last 1) | ConvertFrom-Json
if ($LASTEXITCODE -ne 0) { throw 'Could not inspect the wallpaper host.' }
if (!$inspection.executable) {
    if (Get-AppxPackage '*LivelyWallpaper*') {
        throw 'An existing Microsoft Store Lively installation was detected. Start it once to use it; it has not been replaced.'
    }
    if (!(Get-Command winget.exe -ErrorAction SilentlyContinue)) {
        throw 'Windows App Installer (winget) is required to install Lively automatically.'
    }
    # Windows Package Manager verifies the release hash and resolves WebView2.
    # Do not launch Lively until its first-run configuration has been prepared.
    & winget.exe install --id rocksdanister.LivelyWallpaper --version 2.2.1.0 --exact --source winget --silent --accept-source-agreements --accept-package-agreements --disable-interactivity --override '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /CURRENTUSER /NOAUTOLAUNCH /MERGETASKS=!desktopicon,!windowsstartup'
    if ($LASTEXITCODE -ne 0) { throw "Lively installation failed: $LASTEXITCODE" }
    & $helper prepare
    if ($LASTEXITCODE -ne 0) { throw 'Lively first-run preparation failed.' }
}
