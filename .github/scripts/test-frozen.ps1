$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
$backend = Join-Path $root 'backend-dist/MediaSquirrelBackend'
$exe = Join-Path $backend 'MediaSquirrelBackend.exe'
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $root 'backend-dist/playwright-browsers'
$smoke = Join-Path $root 'build/frozen-smoke.py'
@'
import requests, cv2, runpy, pathlib, sys
from browserforge.headers import HeaderGenerator
from playwright.sync_api import sync_playwright
for name in ('weibo_downloader.py', 'douyin_downloader.py'):
    runpy.run_path(str(pathlib.Path(sys._MEIPASS) / name), run_name='frozen_import_check')
assert any(k.lower() == 'user-agent' for k in HeaderGenerator(browser='chrome').generate())
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    page.set_content('<title>frozen smoke</title>')
    assert page.title() == 'frozen smoke'
    browser.close()
print('Frozen requests, OpenCV, browserforge and bundled browser: PASS')
'@ | Set-Content -LiteralPath $smoke -Encoding ascii
& $exe --internal-run $smoke
if ($LASTEXITCODE -ne 0) { throw 'Frozen runtime check failed' }
python (Join-Path $PSScriptRoot 'test-frozen-backend.py')
if ($LASTEXITCODE -ne 0) { throw 'Frozen backend lifecycle check failed' }
