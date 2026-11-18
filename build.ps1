# build.ps1 - Clean PyInstaller build & isolated Portable ZIP packager (ASCII only)
param(
    [switch]$OneFile,
    [switch]$PackageZip,
    [string]$Version = "2.1.0",
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

if (-not $Python) {
    $venvPy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
    if (Test-Path $venvPy) { $Python = $venvPy }
    else { $Python = "python" }
}

Write-Host "== [1/6] Stop existing instances =="
Get-Process -Name LiquidGlassForWindows, LiquidGlassIconCenter -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Milliseconds 400

Write-Host "== [2/6] Verify Python environment ($Python) =="
& $Python -c "import webview, PIL, numpy, pystray, liquid_glass.web_api; print('imports ok')"
if ($LASTEXITCODE -ne 0) { throw "Python import check failed." }

Write-Host "== [3/6] Clean previous build / dist folders =="
Remove-Item -Recurse -Force .\build, .\dist -ErrorAction SilentlyContinue

if ($OneFile) {
    Write-Host "== [4/6] PyInstaller --onefile build =="
    & $Python -m PyInstaller --noconfirm --clean --onefile --noconsole `
        --icon="assets\app.ico" `
        --add-data "ui;ui" `
        --add-data "windhawk;windhawk" `
        --add-data "assets;assets" `
        --hidden-import liquid_glass.specs --hidden-import liquid_glass.runtime `
        --hidden-import liquid_glass.renderer --hidden-import liquid_glass.windhawk_bridge `
        --hidden-import liquid_glass.shortcut_manager --hidden-import liquid_glass.desktop_watcher `
        --hidden-import liquid_glass.tray_controller --hidden-import liquid_glass.web_api `
        --exclude-module psutil --exclude-module tkinter --exclude-module matplotlib --exclude-module scipy `
        --name="LiquidGlassForWindows" `
        main.py
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller --onefile build failed." }

    $builtExe = Join-Path $PSScriptRoot "dist\LiquidGlassForWindows.exe"
    if (-not (Test-Path $builtExe)) { throw "Built exe not found: $builtExe" }
    Copy-Item -Force $builtExe (Join-Path $PSScriptRoot "LiquidGlassForWindows.exe")
} else {
    Write-Host "== [4/6] PyInstaller --onedir build via spec =="
    & $Python -m PyInstaller --noconfirm --clean .\LiquidGlassForWindows.spec
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller --onedir spec build failed." }

    $builtDir = Join-Path $PSScriptRoot "dist\LiquidGlassForWindows"
    if (-not (Test-Path (Join-Path $builtDir "LiquidGlassForWindows.exe"))) {
        throw "Built dist folder incomplete: $builtDir"
    }
    Remove-Item -Recurse -Force (Join-Path $PSScriptRoot "_internal") -ErrorAction SilentlyContinue
    Copy-Item -Recurse -Force (Join-Path $builtDir "*") $PSScriptRoot
}

if ($PackageZip) {
    if ($OneFile) { throw "-PackageZip requires the standard --onedir build (omit -OneFile)." }
    Write-Host "== [5/6] Create privacy-clean Portable ZIP =="
    $releasesDir = Join-Path $PSScriptRoot "releases"
    New-Item -ItemType Directory -Path $releasesDir -Force | Out-Null
    $zipName = "LiquidGlassForWindows_v${Version}_Portable.zip"
    $zipPath = Join-Path $releasesDir $zipName
    Remove-Item -Force $zipPath -ErrorAction SilentlyContinue

    $stageRoot = Join-Path $PSScriptRoot "build\_portable_stage"
    $stageApp = Join-Path $stageRoot "LiquidGlassForWindows"
    Remove-Item -Recurse -Force $stageRoot -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Path $stageApp -Force | Out-Null

    $distSrc = Join-Path $PSScriptRoot "dist\LiquidGlassForWindows"
    Copy-Item -Force (Join-Path $distSrc "LiquidGlassForWindows.exe") $stageApp
    Copy-Item -Recurse -Force (Join-Path $distSrc "_internal") (Join-Path $stageApp "_internal")

    foreach ($folder in @("ui", "windhawk", "assets")) {
        Copy-Item -Recurse -Force (Join-Path $PSScriptRoot $folder) (Join-Path $stageApp $folder)
    }
    foreach ($file in @("config.example.json", "LICENSE", "README.md")) {
        if (Test-Path (Join-Path $PSScriptRoot $file)) {
            Copy-Item -Force (Join-Path $PSScriptRoot $file) $stageApp
        }
    }
    Get-ChildItem $PSScriptRoot -Filter *.bat | ForEach-Object { Copy-Item -Force $_.FullName $stageApp }
    Get-ChildItem $PSScriptRoot -Filter *.md | ForEach-Object {
        if (-not (Test-Path (Join-Path $stageApp $_.Name))) {
            Copy-Item -Force $_.FullName $stageApp
        }
    }

    Compress-Archive -Path $stageApp -DestinationPath $zipPath -CompressionLevel Optimal -Force

    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip = [System.IO.Compression.ZipFile]::OpenRead($zipPath)
    try {
        $forbidden = @("Original_Shortcuts_Backup", "IconVault", "config.json", ".webview_profile", ".git/", "__pycache__")
        foreach ($entry in $zip.Entries) {
            foreach ($bad in $forbidden) {
                if ($entry.FullName -like "*$bad*") {
                    throw "Privacy check FAILED: forbidden entry '$($entry.FullName)' found in $zipName!"
                }
            }
        }
        Write-Host ("Portable ZIP verified clean: {0} ({1} entries, {2:N2} MB)" -f $zipName, $zip.Entries.Count, ((Get-Item $zipPath).Length / 1MB))
    } finally {
        $zip.Dispose()
    }
} else {
    Write-Host "== [5/6] Skipping Portable ZIP (pass -PackageZip to create release archive) =="
}

Write-Host "== [6/6] Clean temporary build artifacts =="
Remove-Item -Recurse -Force .\build, .\dist -ErrorAction SilentlyContinue
Get-Item (Join-Path $PSScriptRoot "LiquidGlassForWindows.exe") | Select-Object Name, Length, LastWriteTime
