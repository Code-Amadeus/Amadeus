param(
  [Parameter(Mandatory=$true)][string]$TargetsFile,
  [Parameter(Mandatory=$true)][string]$OutputDirectory
)
$ErrorActionPreference = 'Stop'
$stopFile = Join-Path $OutputDirectory 'stop'
$writer = New-Object System.IO.StreamWriter((Join-Path $OutputDirectory 'gpu-memory.ndjson'), $false, (New-Object System.Text.UTF8Encoding($false)))
$writer.AutoFlush = $true
$paths = @(
  '\GPU Process Memory(*)\Dedicated Usage', '\GPU Process Memory(*)\Shared Usage',
  '\GPU Process Memory(*)\Total Committed',
  '\GPU Adapter Memory(*)\Dedicated Usage', '\GPU Adapter Memory(*)\Shared Usage',
  '\Process(*)\ID Process', '\Process(*)\Working Set - Private', '\Process(*)\Private Bytes'
)
$ready = $false
try {
  while (-not (Test-Path -LiteralPath $stopFile)) {
    $started = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
    $target = $null
    if (Test-Path -LiteralPath $TargetsFile) {
      try { $target = Get-Content -LiteralPath $TargetsFile -Raw | ConvertFrom-Json }
      catch { } # A target-file write may overlap this read; the row records its absence.
    }
    $wanted = @($target.allPids)
    $gpu = @{}; $adapters = @{}; $processes = @{}; $invalid = 0
    # Wildcard process counters can include an exited/protected process. Keep
    # valid samples and record invalid statuses instead of discarding all GPUs.
    $counterErrors = @()
    $sample = Get-Counter -Counter $paths -ErrorAction SilentlyContinue -ErrorVariable counterErrors
    if (-not $sample) { throw 'No Windows performance-counter samples returned' }
    foreach ($counter in $sample.CounterSamples) {
      if ($counter.Status -notin @(0,1)) { $invalid++; continue }
      $name = $counter.InstanceName
      $path = $counter.Path.ToLowerInvariant()
      $metric = $path.Substring($path.LastIndexOf('\') + 1)
      $value = [long]$counter.CookedValue
      if ($path.Contains('\gpu process memory(')) {
        if ($name -notmatch '^pid_(\d+)_') { continue }
        $processId = [int]$Matches[1]
        if ($processId -notin $wanted) { continue }
        if (-not $gpu.ContainsKey($name)) { $gpu[$name] = @{pid=$processId;instance=$name} }
        switch ($metric) {
          'dedicated usage' { $gpu[$name].dedicatedBytes = $value }
          'shared usage' { $gpu[$name].sharedBytes = $value }
          'total committed' { $gpu[$name].committedBytes = $value }
        }
      } elseif ($path.Contains('\gpu adapter memory(')) {
        if (-not $adapters.ContainsKey($name)) { $adapters[$name] = @{instance=$name} }
        if ($metric -eq 'dedicated usage') { $adapters[$name].dedicatedBytes = $value }
        if ($metric -eq 'shared usage') { $adapters[$name].sharedBytes = $value }
      } else {
        # InstanceName omits duplicate-process # suffixes on this Windows build.
        # Full counter instance paths preserve the distinct Electron PIDs.
        $processKey = $path.Substring(0, $path.LastIndexOf('\'))
        if (-not $processes.ContainsKey($processKey)) { $processes[$processKey] = @{} }
        switch ($metric) {
          'id process' { $processes[$processKey].pid = $value }
          'working set - private' { $processes[$processKey].privateWorkingSetBytes = $value }
          'private bytes' { $processes[$processKey].privateCommitBytes = $value }
        }
      }
    }
    $row = @{
      atEpochMs=$started; sampleMs=([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() - $started)
      target=$target; invalidCounterValues=$invalid; counterWarnings=@($counterErrors | ForEach-Object {$_.Exception.Message})
      processGpu=@($gpu.Values); adapters=@($adapters.Values)
      processRam=@($processes.Values | Where-Object { $_.pid -in $wanted })
    }
    $writer.WriteLine(($row | ConvertTo-Json -Depth 7 -Compress))
    if (-not $ready -and $adapters.Count -gt 0 -and $processes.Count -gt 0) {
      [System.IO.File]::WriteAllText((Join-Path $OutputDirectory 'ready.json'), '{"ready":true,"units":"bytes","source":"Windows PDH"}')
      $ready = $true
    }
    for ($i=0; $i -lt 10 -and -not (Test-Path -LiteralPath $stopFile); $i++) { Start-Sleep -Milliseconds 200 }
  }
} catch {
  $writer.WriteLine((@{atEpochMs=[DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds();error=$_.Exception.Message} | ConvertTo-Json -Compress))
  exit 1
} finally { $writer.Dispose() }
