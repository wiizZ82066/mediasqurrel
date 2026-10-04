# PyInstaller backend packaging -> backend-dist/MediaSquirrelBackend/ (onedir)
# Also prepares playwright chromium -> backend-dist/playwright-browsers/
# NOTE: keep this file ASCII-only (PowerShell 5.1 misparses BOM-less UTF-8 CJK)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Write-Host "[1/3] frontend dist check..." -ForegroundColor Cyan
if (-not (Test-Path "frontend\dist\index.html")) {
    Write-Error "frontend/dist missing. Run: cd frontend; npm run build"
}

Write-Host "[2/3] PyInstaller..." -ForegroundColor Cyan
if (Test-Path backend-dist) { Remove-Item backend-dist -Recurse -Force }
if (Test-Path build) { Remove-Item build -Recurse -Force }
if (Test-Path MediaSquirrelBackend.spec) { Remove-Item MediaSquirrelBackend.spec -Force }

python -m PyInstaller `
  --noconfirm --clean --onedir `
  --name MediaSquirrelBackend `
  --distpath backend-dist --workpath build `
  --console `
  --add-data "scripts_manifest;scripts_manifest" `
  --add-data "weibo_downloader.py;." `
  --add-data "douyin_downloader.py;." `
  --add-data "frontend/dist;frontend/dist" `
  --add-data "app_data/models;app_data/models" `
  --collect-all playwright `
  --collect-all pystray `
  --collect-data browserforge `
  --collect-data apify_fingerprint_datapoints `
  --collect-submodules uvicorn `
  --hidden-import "app" `
  --hidden-import "app.main" `
  --hidden-import "app.config" `
  --hidden-import "app.task_manager" `
  --hidden-import "app.script_registry" `
  --hidden-import "app.media_library" `
  --hidden-import "app.watcher" `
  --hidden-import "app.thumbs" `
  --hidden-import "app.browser" `
  --hidden-import "app.sub_search" `
  --hidden-import "app.douyin_auth" `
  --hidden-import "app.scanners" `
  --hidden-import "app.scanners.weibo" `
  --hidden-import "app.scanners.douyin" `
  --hidden-import "uvicorn.logging" `
  --exclude-module tkinter `
  --exclude-module matplotlib `
  --exclude-module pytest `
  --exclude-module PyQt5 `
  --exclude-module PyQt6 `
  --exclude-module PySide2 `
  --exclude-module PySide6 `
  --exclude-module qtpy `
  run.py
if ($LASTEXITCODE -ne 0) { Write-Error "PyInstaller failed" }

Write-Host "[3/3] playwright chromium..." -ForegroundColor Cyan
$msPw = Join-Path $env:LOCALAPPDATA "ms-playwright"
$dst = "backend-dist\playwright-browsers"
New-Item -ItemType Directory -Force -Path $dst | Out-Null
if (Test-Path $msPw) {
    Get-ChildItem $msPw -Directory -Filter "chromium*" | ForEach-Object {
        Copy-Item $_.FullName (Join-Path $dst $_.Name) -Recurse -Force
        Write-Host ("  + " + $_.Name)
    }
    Get-ChildItem $msPw -Directory -Filter "ffmpeg*" | ForEach-Object {
        Copy-Item $_.FullName (Join-Path $dst $_.Name) -Recurse -Force
        Write-Host ("  + " + $_.Name)
    }
} else {
    Write-Warning "ms-playwright not found; bundled browsers skipped"
}

$size = (Get-ChildItem backend-dist -Recurse | Measure-Object Length -Sum).Sum / 1MB
Write-Host ("=== DONE: backend-dist ({0:N0} MB) ===" -f $size)
