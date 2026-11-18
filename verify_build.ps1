# verify_build.ps1 - Sandboxed EXE smoke verification (never touches real Desktop or Windhawk; ASCII only)
param(
    [string]$ExePath = ".\LiquidGlassForWindows.exe",
    [int]$WaitSeconds = 4
)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

$resolvedExe = (Resolve-Path $ExePath).Path
$sandbox = Join-Path $env:TEMP ("lgfw_verify_" + $PID)
$whMissing = Join-Path $sandbox "absent_wh"
New-Item -ItemType Directory -Path $sandbox -Force | Out-Null

# Pre-seed sandbox config with exit_when_window_closes=true so GUI check does not spawn a tray process
$seedCfg = @'
{
  "version": "2.1.0",
  "watcher": {"enabled": false, "debounce_sec": 3.0},
  "lifecycle": {"restore_live_on_exit": false, "exit_when_window_closes": true}
}
'@
Set-Content -Path (Join-Path $sandbox "config.json") -Value $seedCfg -Encoding UTF8

$prevBase = $env:LGFW_BASE_DIR
$prevWh = $env:LGFW_WINDHAWK_DIR
$env:LGFW_BASE_DIR = $sandbox
$env:LGFW_WINDHAWK_DIR = $whMissing

$proc = $null
try {
    Write-Host "== [1/2] Verify bundled imports & assets via --check =="
    $checkProc = Start-Process -FilePath $resolvedExe -ArgumentList "--check" -WorkingDirectory (Split-Path $resolvedExe) -Wait -PassThru -NoNewWindow
    if ($checkProc.ExitCode -ne 0) {
        throw "Frozen self-check (--check) failed with exit code: $($checkProc.ExitCode)"
    }
    Write-Host "Self-check (--check): PASS"

    Write-Host "== [2/2] Verify GUI EXE startup (--settings) stays alive for ${WaitSeconds}s =="
    $proc = Start-Process -FilePath $resolvedExe -ArgumentList "--settings" -WorkingDirectory (Split-Path $resolvedExe) -PassThru
    Start-Sleep -Seconds $WaitSeconds
    if ($proc.HasExited) {
        throw "GUI process exited prematurely with code $($proc.ExitCode)"
    }
    Write-Host "GUI startup check: PASS (PID=$($proc.Id))"
} finally {
    if ($proc -and -not $proc.HasExited) {
        Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
        Start-Sleep -Milliseconds 300
    }
    Get-Process -Name LiquidGlassForWindows -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    $env:LGFW_BASE_DIR = $prevBase
    $env:LGFW_WINDHAWK_DIR = $prevWh
    Remove-Item -Recurse -Force $sandbox -ErrorAction SilentlyContinue
}
Write-Host "ALL EXE VERIFICATION CHECKS PASSED"
