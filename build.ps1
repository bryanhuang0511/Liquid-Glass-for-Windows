param([switch]$OneFile)
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

Write-Host "== [1/5] Stop existing processes =="
Get-Process -Name LiquidGlassIconCenter -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Milliseconds 500

Write-Host "== [2/5] Clean build artifacts =="
Remove-Item -Recurse -Force .\build, .\dist -ErrorAction SilentlyContinue

$mode = "--onedir"
if ($OneFile) { $mode = "--onefile" }
Write-Host "== [3/5] PyInstaller $mode build =="
python -m PyInstaller --noconfirm --clean $mode --noconsole `
    --icon="LiquidGlassIconCenter.ico" `
    --add-data "ui;ui" `
    --add-data "windhawk;windhawk" `
    --add-data "LiquidGlassIconCenter.png;." `
    --hidden-import glass_render --hidden-import glass_specs --hidden-import windhawk_bridge `
    --exclude-module psutil --exclude-module tkinter --exclude-module matplotlib --exclude-module scipy `
    --name="LiquidGlassIconCenter" `
    LiquidGlassIconCenter.py

if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller build failed!"
}

Write-Host "== [4/5] Move output next to project files =="
if ($OneFile) {
    $src = Join-Path $PSScriptRoot "dist\LiquidGlassIconCenter.exe"
    if (-not (Test-Path $src)) { throw "Build output not found at: $src" }
    Copy-Item -Force $src (Join-Path $PSScriptRoot "LiquidGlassIconCenter.exe")
} else {
    $srcDir = Join-Path $PSScriptRoot "dist\LiquidGlassIconCenter"
    if (-not (Test-Path $srcDir)) { throw "Build output not found at: $srcDir" }
    Remove-Item -Recurse -Force (Join-Path $PSScriptRoot "_internal") -ErrorAction SilentlyContinue
    Copy-Item -Recurse -Force (Join-Path $srcDir "*") $PSScriptRoot
}
Remove-Item -Recurse -Force .\build, .\dist, .\LiquidGlassIconCenter.spec -ErrorAction SilentlyContinue

Write-Host "== [5/5] Build finished! =="
Get-Item (Join-Path $PSScriptRoot "LiquidGlassIconCenter.exe") | Select-Object Name, Length, LastWriteTime
