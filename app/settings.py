"""Validated local preferences and diagnostics with separate verification levels."""
import ast
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time

from . import config, db
from .archive import atomic_write_text
from .redaction import redact_text, redact_value

_LOCK = threading.RLock()
_storage_cached = None
DEFAULTS = {'default_download_dir': '', 'download_concurrency': 1, 'timeout_seconds': 120,
            'retries': 2, 'cache_limit_mb': 2048, 'notifications': 'in_app',
            'density': 'comfortable', 'reduce_motion': True}
_RANGES = {'download_concurrency': (1, 4), 'timeout_seconds': (10, 600), 'retries': (0, 5), 'cache_limit_mb': (128, 32768)}


def load():
    path = Path(config.DATA_DIR) / 'settings.json'
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(data, dict):
            raise ValueError('设置文件格式不正确')
        result = {**DEFAULTS, **{key: value for key, value in data.items() if key in DEFAULTS}}
    except FileNotFoundError:
        result = dict(DEFAULTS)
    return validate(result)


def validate(values):
    result = dict(values)
    unknown = set(result) - set(DEFAULTS)
    if unknown:
        raise ValueError('不支持的设置字段：' + ', '.join(sorted(unknown)))
    for name, (lower, upper) in _RANGES.items():
        if name in result and (isinstance(result[name], bool) or not isinstance(result[name], int) or not lower <= result[name] <= upper):
            raise ValueError(f'{name} 必须是 {lower}–{upper} 的整数')
    if result.get('notifications', 'in_app') != 'in_app':
        raise ValueError('本轮使用网页内通知')
    if result.get('density', 'comfortable') not in {'comfortable', 'compact'}:
        raise ValueError('列表密度无效')
    if result.get('reduce_motion', True) is not True:
        raise ValueError('本轮只保留进度条动画')
    output = result.get('default_download_dir', '')
    if not isinstance(output, str):
        raise ValueError('默认下载目录格式不正确')
    if output:
        if not Path(output).is_absolute():
            raise ValueError('默认下载目录需要绝对路径')
        chosen = Path(output).resolve()
        if chosen.is_relative_to(Path(config.SOURCE_DIR).resolve()):
            raise ValueError('新媒体不能保存在项目目录内')
        if not chosen.is_relative_to(Path(config.LIBRARY_ROOT).resolve()):
            raise ValueError('更换媒体根目录请使用数据迁移预检；默认下载子目录应位于当前媒体库内')
        result['default_download_dir'] = str(chosen)
    return result


def apply(values):
    config.MAX_CONCURRENT_TASKS = values['download_concurrency']
    config.DOWNLOAD_TIMEOUT = values['timeout_seconds']
    config.DOWNLOAD_RETRIES = values['retries']
    config.CACHE_LIMIT_BYTES = values['cache_limit_mb'] * 1024 * 1024
    config.DEFAULT_DOWNLOAD_DIR = values['default_download_dir'] or None


def save(changes):
    with _LOCK:
        values = validate({**load(), **changes})
        atomic_write_text(Path(config.DATA_DIR) / 'settings.json', json.dumps(values, ensure_ascii=False, indent=2))
        apply(values)
        return values


def versions():
    result = {'python': sys.version.split()[0]}
    for name in ('fastapi', 'uvicorn', 'playwright', 'opencv-python', 'requests', 'pystray', 'Pillow', 'tzdata'):
        try:
            result[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            result[name] = None
    node = shutil.which('node')
    if node:
        try:
            result['node'] = subprocess.check_output([node, '--version'], timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0).decode().strip()
        except (OSError, subprocess.SubprocessError):
            result['node'] = None
    return result


def storage_usage():
    global _storage_cached
    now = time.monotonic()
    if _storage_cached and _storage_cached[0] == config.DATA_DIR and now - _storage_cached[1] < 60:
        return _storage_cached[2]
    database_bytes = sum(Path(config.DB_PATH + suffix).stat().st_size
                         for suffix in ('', '-wal', '-shm') if Path(config.DB_PATH + suffix).is_file())
    with db.connect() as connection:
        archived = connection.execute('SELECT coalesce(sum(bytes),0) FROM task_log_archives').fetchone()[0]
        tasks = connection.execute('SELECT count(*) FROM tasks').fetchone()[0]
    free = shutil.disk_usage(config.DATA_DIR).free
    value = {'database_bytes': database_bytes, 'archived_log_bytes': archived, 'task_count': tasks,
             'free_bytes': free, 'low_space': free < 512 * 1024 * 1024,
             'cache_limit_bytes': config.CACHE_LIMIT_BYTES}
    _storage_cached = (config.DATA_DIR, now, value)
    return value


def diagnostics():
    checks = []
    def add(identity, label, status, reason, suggestion=''):
        checks.append(dict(id=identity, label=label, status=status, reason=reason, suggestion=suggestion))
    try:
        code = 'import fastapi, uvicorn, playwright.sync_api, requests, cv2, PIL, pystray, browserforge; import weibo_downloader, douyin_downloader'
        tested = subprocess.run([sys.executable, '-B', '-c', code], cwd=config.RESOURCE_DIR,
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=20,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        if tested.returncode:
            add('dependencies', 'Python 依赖与脚本导入', 'failed', redact_text(tested.stderr[-2000:], diagnostic=True), '在当前 Python 环境执行 python -m pip install -r requirements.txt')
        else:
            add('dependencies', 'Python 依赖与脚本导入', 'passed', '已在独立子进程实际导入依赖和两个下载脚本')
    except (OSError, subprocess.SubprocessError) as error:
        add('dependencies', 'Python 依赖与脚本导入', 'failed', redact_text(str(error), diagnostic=True), '检查 Python 环境与依赖安装')
    for script in ('weibo_downloader.py', 'douyin_downloader.py'):
        try:
            ast.parse((Path(config.RESOURCE_DIR) / script).read_text(encoding='utf-8'))
            add(script, script, 'passed', '入口存在且 Python 语法解析通过；联网可用性仍需单独测试')
        except (OSError, SyntaxError) as error:
            add(script, script, 'failed', redact_text(str(error), diagnostic=True), '恢复脚本源文件后重试')
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            bundled = Path(playwright.chromium.executable_path).is_file()
            # executable_path is local metadata; a protocol round trip allows
            # the driver initialization to settle before stopping on Python 3.14.
            probe = playwright.request.new_context()
            probe.dispose()
        add('chromium', 'Playwright Chromium 文件', 'passed' if bundled else 'failed',
            '浏览器文件存在；尚未验证启动' if bundled else '未找到 Playwright Chromium',
            '' if bundled else '执行 python -m playwright install chromium')
    except Exception as error:
        add('chromium', 'Playwright Chromium 文件', 'failed', redact_text(str(error), diagnostic=True), '检查 Playwright 安装')
    from . import douyin_auth
    saved = douyin_auth.load_cookies()
    add('douyin_login', '抖音登录态', 'unverified', '有本地登录信息，平台是否仍接受尚未联网验证' if saved else '未保存登录信息', '需要时在订阅页打开登录窗口')
    from . import weibo_auth
    weibo_saved = weibo_auth.load_cookies()
    add('weibo_login', '微博登录态', 'unverified', '有本地登录信息，平台是否仍接受尚未联网验证' if weibo_saved else '未保存登录信息；公开主页可能允许访客访问', '订阅扫描提示需要登录时，在订阅页点击登录微博；下载可用性另做独立检测')
    add('network_download', '真实联网下载', 'not_run', '不会自动访问平台或使用历史链接', '输入测试链接并确认后，结果保存在独立检测目录')
    return {'checks': checks, 'versions': versions()}


def check_browser():
    results = []
    from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        for label, options in (('Playwright Chromium', {}), ('系统 Chrome', {'channel': 'chrome'})):
            browser = None
            try:
                browser = playwright.chromium.launch(headless=True, timeout=15000, **options)
                page = browser.new_page()
                page.goto('about:blank')
                results.append({'id': label, 'label': label, 'status': 'passed', 'reason': '浏览器可启动并创建空白页面；未访问外部网站'})
            except Exception as error:
                results.append({'id': label, 'label': label, 'status': 'failed', 'reason': redact_text(str(error), diagnostic=True),
                                'suggestion': '安装对应浏览器或修复 Playwright 环境；抖音在 Chrome 不可用时会尝试 Chromium'})
            finally:
                if browser:
                    browser.close()
    return {'checks': results}
