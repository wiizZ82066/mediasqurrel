# PyInstaller backend packaging -> backend-dist/MediaSquirrelBackend/ (onedir)
# Also prepares playwright chromium -> backend-dist/playwright-browsers/
# NOTE: keep this file ASCII-only (PowerShell 5.1 misparses BOM-less UTF-8 CJK)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# The only browser revision required by the pinned playwright version.
# Determined from playwright driver's browsers.json (chromium/headless-shell/ffmpeg).
$browserJson = python -c "import pathlib,playwright; print((pathlib.Path(playwright.__file__).parent/'driver/package/browsers.json').read_text())"
if ($LASTEXITCODE -ne 0) { throw "Cannot read pinned Playwright browser metadata" }
$browserData = ($browserJson -join "`n") | ConvertFrom-Json
$RequiredRevisions = foreach ($name in @("chromium", "chromium-headless-shell", "ffmpeg")) {
    $browser = $browserData.browsers | Where-Object { $_.name -eq $name } | Select-Object -First 1
    if (-not $browser) { throw "Browser metadata missing: $name" }
    ($name.Replace('-', '_') + '-' + $browser.revision)
}

Write-Host "[1/3] frontend dist check..." -ForegroundColor Cyan
if (-not (Test-Path "frontend\dist\index.html")) {
    Write-Error "frontend/dist missing. Run: cd frontend; npm run build"
}

Write-Host "[2/3] PyInstaller..." -ForegroundColor Cyan
foreach ($name in @('backend-dist', 'build')) {
    $target = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot $name))
    if ($target -ne (Join-Path $PSScriptRoot $name)) { throw "Unexpected build path: $target" }
    if (Test-Path -LiteralPath $target) {
        if ((Get-Item -LiteralPath $target).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "Build path is a reparse point" }
        Remove-Item -LiteralPath $target -Recurse -Force
    }
}
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
$msPw = $env:PLAYWRIGHT_BROWSERS_PATH
if (-not $msPw) { $msPw = Join-Path $env:LOCALAPPDATA "ms-playwright" }
$msPw = [IO.Path]::GetFullPath($msPw)
$dst = "backend-dist\playwright-browsers"
New-Item -ItemType Directory -Force -Path $dst | Out-Null
if (Test-Path $msPw) {
    foreach ($rev in $RequiredRevisions) {
        $srcDir = Join-Path $msPw $rev
        if (Test-Path $srcDir) {
            Copy-Item $srcDir (Join-Path $dst $rev) -Recurse -Force
            Write-Host ("  + " + $rev)
        } else {
            throw ("Required browser revision missing: " + $srcDir)
        }
    }
} else {
    throw "Playwright browser directory missing: $msPw"
}

$size = (Get-ChildItem backend-dist -Recurse -File | Measure-Object Length -Sum).Sum / 1MB
Write-Host ("=== DONE: backend-dist ({0:N0} MB) ===" -f $size)
