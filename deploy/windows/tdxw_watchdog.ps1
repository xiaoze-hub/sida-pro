<#
.SYNOPSIS
  TdxW (Tongdaxin) TQ-gateway watchdog for the Windows mini host.

.DESCRIPTION
  Probes the TQ gateway with a REAL JSON-RPC call (method get_market_snapshot)
  and requires a nonzero price ("Now") in the reply. A port-only check can NOT
  detect the "fake-alive" zombie (TCP 17709 still LISTENING but JSON-RPC empty),
  so this watchdog always validates a live quote.

  On 2 CONSECUTIVE failed rounds it kills every TdxW process, relaunches the
  TdxAutoStart scheduled task, waits, then re-probes. The 2-round hysteresis
  leaves a ~10 minute window (5 min cadence) so the remembered-password login
  screen (process mem 200MB+, no quote yet) is NOT killed by mistake. It also
  refuses to kill while TdxW owns a visible main window.

.PARAMETER DryRun
  Probe + decide + log only. Never kills or relaunches. Use for self-test.

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File C:\Users\tianxiang\tdxw_watchdog.ps1 -DryRun

.NOTES
  ASCII ONLY: Windows PowerShell 5.1 parses .ps1 as GBK, so non-ASCII source
  (Chinese comments/labels) corrupts the syntax. Keep every character ASCII.
  Invoked over SSH inside a piped script, keep inner commands from eating stdin
  (redirect </dev/null) -- the same stdin is shared, and a consumer silently
  truncates the whole script.
#>
param(
  [switch]$DryRun
)

$ErrorActionPreference = 'SilentlyContinue'

# --- config ---------------------------------------------------------------
$RpcUrl        = 'http://127.0.0.1:17709/'
$ProbeCode     = '002361.SZ'                     # a normal, always-listed stock
$ProbeTimeout  = 12                              # seconds for the JSON-RPC probe
$TaskName      = 'TdxAutoStart'                  # scheduled task that launches TdxW.exe
$FailThreshold = 2                               # consecutive fails before acting
$RelaunchWait  = 90                              # seconds to wait after relaunch
$LogPath       = 'C:\Users\tianxiang\tqwatch.log'
$StatePath     = 'C:\Users\tianxiang\tqwatch_state.txt'
$LogMaxBytes   = 2MB

# --- helpers --------------------------------------------------------------
function Write-Log {
  param([string]$Message)
  $ts = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')
  "$ts [tdxw-watchdog] $Message" | Out-File -FilePath $LogPath -Append -Encoding ascii
}

function Rotate-Log {
  try {
    if ((Test-Path $LogPath) -and ((Get-Item $LogPath).Length -gt $LogMaxBytes)) {
      Move-Item -Force $LogPath ($LogPath + '.1')
    }
  } catch {
    # rotation is best-effort; never block the probe
  }
}

function Read-Fails {
  try {
    if (Test-Path $StatePath) {
      $raw = (Get-Content $StatePath -ErrorAction SilentlyContinue | Select-Object -First 1)
      $n = 0
      if ([int]::TryParse(([string]$raw).Trim(), [ref]$n)) { return $n }
    }
  } catch {
    # fall through
  }
  return 0
}

function Write-Fails {
  param([int]$Count)
  Set-Content -Path $StatePath -Value ([string]$Count) -Encoding ascii
}

# Real JSON-RPC liveness test: true ONLY when the reply carries a nonzero price.
function Test-TqRpc {
  $body = '{"id":1,"method":"get_market_snapshot","params":{"stock_code":"' + $ProbeCode + '"}}'
  try {
    $resp = Invoke-RestMethod -Uri $RpcUrl -Method Post `
              -ContentType 'application/json; charset=utf-8' `
              -Body $body -TimeoutSec $ProbeTimeout
  } catch {
    return $false
  }
  if ($null -eq $resp) { return $false }
  $node = $resp.result
  if ($null -eq $node) { return $false }
  # some methods nest the payload under result.Value; get_market_snapshot is flat.
  if ($null -ne $node.Value -and ($node.Value.PSObject.Properties.Name -contains 'Now')) {
    $node = $node.Value
  }
  $err = [string]$node.ErrorId
  if ($err -ne '' -and $err -ne '0') { return $false }
  if ($null -eq $node.Now) { return $false }
  $price = 0.0
  if (-not [double]::TryParse(([string]$node.Now), [ref]$price)) { return $false }
  if ($price -le 0) { return $false }
  return $true
}

# --- main -----------------------------------------------------------------
Rotate-Log
$fails = Read-Fails
$tdxw = Get-Process -Name TdxW -ErrorAction SilentlyContinue |
          Sort-Object WorkingSet64 -Descending | Select-Object -First 1
$memMB = 0
if ($tdxw) { $memMB = [int][math]::Round($tdxw.WorkingSet64 / 1MB) }

if (Test-TqRpc) {
  Write-Log ("OK: get_market_snapshot returns price (memMB={0})" -f $memMB)
  Write-Fails 0
  exit 0
}

$fails = $fails + 1
Write-Log ("FAIL: JSON-RPC has no price (memMB={0}, consecutive={1}/{2})" -f $memMB, $fails, $FailThreshold)
if ($fails -lt $FailThreshold) {
  Write-Fails $fails
  Write-Log 'WAIT: below threshold, not acting yet (login-screen window honoured)'
  exit 0
}

# Threshold reached. Never kill while a real main window still exists
# (a visible client + no quote yet is the remembered-password login screen).
$hasWindow = $false
if ($tdxw -and $tdxw.MainWindowHandle -ne 0) { $hasWindow = $true }
if ($hasWindow) {
  Write-Log 'WAIT: TdxW owns a main window, not killing (avoid login-screen kill loop)'
  Write-Fails 0
  exit 0
}

if ($DryRun) {
  Write-Log 'DRYRUN: would taskkill TdxW.exe /T (+ tdxcef.exe), run schtasks TdxAutoStart, then re-probe'
  Write-Fails 0
  exit 0
}

Write-Log 'ACTION: threshold reached + no window -> kill TdxW, relaunch, re-probe'
& taskkill.exe /F /IM TdxW.exe /T 2>&1 | Out-Null
Start-Sleep -Seconds 3
& taskkill.exe /F /IM tdxcef.exe /T 2>&1 | Out-Null
Start-Sleep -Seconds 2
& schtasks.exe /run /tn $TaskName 2>&1 | Out-Null
Start-Sleep -Seconds $RelaunchWait

if (Test-TqRpc) {
  Write-Log 'RECOVERED: get_market_snapshot returns price after relaunch'
  Write-Fails 0
  exit 0
}

Write-Log 'STILL-DOWN: relaunch did not restore JSON-RPC; escalation (manual login / rebootfix) needed'
# Reset the counter: give the freshly relaunched client a full new window before
# acting again, so a 5-min cadence cannot turn into a kill loop.
Write-Fails 0
exit 1
