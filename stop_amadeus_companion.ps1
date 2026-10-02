$ErrorActionPreference = 'SilentlyContinue'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$envPath = Join-Path $root '.env'
$token = ''
if (Test-Path -LiteralPath $envPath) {
  $line = Select-String -LiteralPath $envPath -Pattern '^AMADEUS_BACKEND_TOKEN=' | Select-Object -First 1
  if ($line) { $token = ($line.Line -split '=', 2)[1].Trim() }
}
if ($token) {
  try {
    Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:17777/companion/card-close' -Headers @{ 'X-Amadeus-Token' = $token } -TimeoutSec 8 | Out-Null
  } catch {}
}
Start-Sleep -Milliseconds 800
$portPids = Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
  Where-Object { $_.LocalPort -in 17777, 8788, 17878 } |
  Select-Object -ExpandProperty OwningProcess -Unique
foreach ($procId in $portPids) {
  $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$procId" -ErrorAction SilentlyContinue
  if (-not $proc) { continue }
  $cmd = [string]$proc.CommandLine
  $ownsCompanion = $cmd -match 'server\.app.*--companion' -or $cmd -match 'vn_portrait_overlay_lite\.py' -or $cmd -match 'Amadeus-Companion-Work'
  if ($ownsCompanion) { Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue }
}