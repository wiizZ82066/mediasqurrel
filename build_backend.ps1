# PyInstaller backend packaging -> backend-dist/MediaSquirrelBackend/ (onedir)
# Also prepares playwright chromium -> backend-dist/playwright-browsers/
# NOTE: keep this file ASCII-only (PowerShell 5.1 misparses BOM-less UTF-8 CJK)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# The only browser revision required by the pinned playwright version.
# Determined from playwright driver's browsers.json (chromium/headless-shell/ffmpeg).
$RequiredRevisions = @("chromium-1234", "chromium_headless_shell-1234", "ffmpeg-1011")

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
  --add-data "package.json;." `
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
  --exclude-module patchright `
  run.py
if ($LASTEXITCODE -ne 0) { Write-Error "PyInstaller failed" }

# patchright is emergency-only (USE_PATCHRIGHT=False), remove from release
$pr = "backend-dist\MediaSquirrelBackend\_internal\patchright"
if (Test-Path $pr) {
    Remove-Item $pr -Recurse -Force
    Write-Host "  - removed patchright (saves ~88 MB)"
}

Write-Host "[3/3] playwright chromium (required revisions only)..." -ForegroundColor Cyan
$msPw = Join-Path $env:LOCALAPPDATA "ms-playwright"
$dst = "backend-dist\playwright-browsers"
New-Item -ItemType Directory -Force -Path $dst | Out-Null
if (Test-Path $msPw) {
    foreach ($rev in $RequiredRevisions) {
        $srcDir = Join-Path $msPw $rev
        if (Test-Path $srcDir) {
            Copy-Item $srcDir (Join-Path $dst $rev) -Recurse -Force
            Write-Host ("  + " + $rev)
        } else {
            Write-Warning ("required revision missing on this machine: " + $rev)
        }
    }
} else {
    Write-Warning "ms-playwright not found; bundled browsers skipped"
}

$size = (Get-ChildItem backend-dist -Recurse -File | Measure-Object Length -Sum).Sum / 1MB
Write-Host ("=== DONE: backend-dist ({0:N0} MB) ===" -f $size)
