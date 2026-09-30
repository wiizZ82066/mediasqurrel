# PyInstaller 打包脚本（后端 sidecar）
# 产物: backend-dist/media-squirrel-backend/ (onedir, 启动快)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# 清理旧产物
if (Test-Path backend-dist) { Remove-Item backend-dist -Recurse -Force }
if (Test-Path build) { Remove-Item build -Recurse -Force }
if (Test-Path media-squirrel-backend.spec) { Remove-Item media-squirrel-backend.spec -Force }

python -m PyInstaller `
  --noconfirm `
  --clean `
  --onedir `
  --name media-squirrel-backend `
  --distpath backend-dist `
  --workpath build `
  --console `
  --add-data "scripts_manifest;scripts_manifest" `
  --add-data "weibo_downloader.py;." `
  --add-data "douyin_downloader.py;." `
  --add-data "frontend/dist;frontend/dist" `
  --collect-all scrapling `
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
  --hidden-import "app.scanners" `
  --hidden-import "app.scanners.weibo" `
  --hidden-import "app.scanners.douyin" `
  --hidden-import "uvicorn.logging" `
  --hidden-import "engineio.async_drivers" `
  --exclude-module tkinter `
  --exclude-module matplotlib `
  --exclude-module pytest `
  --exclude-module PyQt5 `
  --exclude-module PyQt6 `
  --exclude-module PySide2 `
  --exclude-module PySide6 `
  --exclude-module qtpy `
  run.py

Write-Host "=== 打包完成 ==="
Get-ChildItem backend-dist\media-squirrel-backend -Name | Select-Object -First 10
