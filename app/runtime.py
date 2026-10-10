"""Source-launch preflight without automatic dependency installation."""
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import urllib.request
from urllib.parse import unquote, urlsplit

from . import config


def instance_id():
    return hashlib.sha256(os.path.normcase(os.path.realpath(config.DATA_DIR)).encode()).hexdigest()[:20]


def runtime_identity(source=None):
    """Fingerprint only runtime inputs, separate from the data-directory lock.

    The server captures this once while importing its application. Calling it
    from a health request would wrongly identify old loaded code as new code.
    """
    root = Path(source or config.SOURCE_DIR).resolve()
    files = {root / name for name in (
        'run.py', 'weibo_downloader.py', 'douyin_downloader.py',
        'package.json', 'requirements.txt', 'app/weibo_emoticons.json',
    )}
    for directory, pattern in (('app', '*.py'), ('scripts_manifest', '*.json')):
        files.update(path for path in (root / directory).rglob(pattern)
                     if not any(part.startswith('.') or part in ('tests', '__pycache__')
                                for part in path.relative_to(root / directory).parts))
    digest = hashlib.sha256()
    for path in sorted(files):
        if path.is_file():
            if path.is_symlink() or not path.resolve().is_relative_to(root):
                raise RuntimeError('运行源码不能引用源码目录外的文件')
            digest.update(path.relative_to(root).as_posix().encode('utf-8') + b'\0')
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    digest.update(b'frontend\0' + frontend_signature(root / 'frontend').encode('ascii'))
    source_id = hashlib.sha256(os.path.normcase(str(root)).encode('utf-8')).hexdigest()[:20]
    return {'instance_id': instance_id(), 'source_id': source_id, 'code_signature': digest.hexdigest()}


def existing_instance_problem(existing, expected):
    """None means safe reuse; missing legacy identity is explicitly unknown."""
    if existing.get('instance_id') != expected['instance_id']:
        return '此端口的 Media Squirrel 正在使用另一份数据，请先退出旧实例或选择其他端口。'
    if not existing.get('source_id') or not existing.get('code_signature'):
        reason = '正在运行的旧实例未提供启动代码标识，无法确认它是否包含本次修改。'
    elif existing['source_id'] != expected['source_id']:
        reason = '正在运行的 Media Squirrel 来自另一份源码目录。'
    elif existing['code_signature'] != expected['code_signature']:
        reason = '当前源码已更新，但后台仍在运行启动时的旧代码。'
    else:
        return None
    return reason + '请等待任务结束，从系统托盘选择“退出”（控制台模式按 Ctrl+C），再重新运行启动器。关闭网页不会停止服务。'


def probe_server(port):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f'http://127.0.0.1:{port}/api/health', timeout=1) as response:
            value = json.loads(response.read(8192))
        if isinstance(value, dict) and value.get('application') == 'media-squirrel':
            return value
    except (OSError, ValueError):
        pass
    return None


def port_in_use(port):
    with socket.socket() as check:
        try:
            check.bind(('127.0.0.1', port))
            return False
        except OSError:
            return True


def frontend_signature(frontend=None):
    root = Path(frontend or config.SOURCE_DIR) if frontend else Path(config.SOURCE_DIR) / 'frontend'
    files = [root / name for name in ('index.html', 'package.json', 'package-lock.json', 'vite.config.js')]
    for directory in ('src', 'public'):
        if (root / directory).exists():
            files.extend(path for path in (root / directory).rglob('*') if path.is_file())
    digest = hashlib.sha256()
    for path in sorted(files):
        if path.is_file():
            digest.update(path.relative_to(root).as_posix().encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def _build_files(root):
    """Inventory includes lazy chunks, which are absent from index.html."""
    dist = root / 'dist'
    if not (dist / 'index.html').is_file() or not (dist / 'assets').is_dir():
        raise ValueError('前端入口或 assets 目录缺失')
    class References(HTMLParser):
        def handle_starttag(self, tag, attrs):
            values = dict(attrs)
            reference = values.get('src') if tag == 'script' else values.get('href') if tag == 'link' else None
            if not reference:
                return
            url = urlsplit(reference)
            if url.scheme or url.netloc:
                return
            path = (dist / unquote(url.path).lstrip('/')).resolve()
            if not path.is_relative_to(dist.resolve()) or not path.is_file():
                raise ValueError('前端入口引用的资源缺失')
    References().feed((dist / 'index.html').read_text(encoding='utf-8'))
    files = {}
    for path in dist.rglob('*'):
        if path.is_file() and path.name not in ('.source-hash', '.build-manifest.json'):
            if path.is_symlink() or not path.resolve().is_relative_to(dist.resolve()):
                raise ValueError('前端资源不能指向构建目录外')
            files[path.relative_to(dist).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
            if len(files) > 10000:
                raise ValueError('前端构建资源数量异常')
    return files


def frontend_build_valid(root):
    try:
        manifest = root / 'dist' / '.build-manifest.json'
        if manifest.stat().st_size > 2 * 1024 * 1024:
            return False
        expected = json.loads(manifest.read_text(encoding='utf-8'))
        return bool(expected) and expected == _build_files(root)
    except (OSError, ValueError):
        return False


def ensure_frontend(*, build=True, frontend=None):
    root = Path(frontend) if frontend else Path(config.SOURCE_DIR) / 'frontend'
    if getattr(sys, 'frozen', False) or os.environ.get('MS_DEV') == '1':
        return 'external'
    signature = frontend_signature(root)
    stamp = root / 'dist' / '.source-hash'
    if stamp.is_file() and stamp.read_text().strip() == signature and frontend_build_valid(root):
        return 'current'
    if not build:
        return 'stale'
    npm = shutil.which('npm.cmd' if os.name == 'nt' else 'npm')
    if not npm or not (root / 'node_modules' / 'vite').is_dir():
        raise RuntimeError('前端需要构建。请安装兼容的 Node.js，然后在 frontend 目录执行 npm ci，再重新启动。')
    print('[startup] 前端源码有变化或构建资源不完整，正在构建…')
    subprocess.run([npm, 'run', 'build'], cwd=root, check=True, timeout=180,
                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    try:
        files = _build_files(root)
    except (OSError, ValueError) as error:
        raise RuntimeError('前端构建资源不完整，请检查 npm run build 的输出') from error
    (root / 'dist' / '.build-manifest.json').write_text(json.dumps(files, sort_keys=True), encoding='utf-8')
    stamp.write_text(signature + '\n', encoding='utf-8')
    return 'built'


class InstanceLock:
    """The same data directory may only have one running writer process."""
    def __init__(self, directory=None):
        self.path = Path(directory or config.DATA_DIR) / '.instance.lock'
        self.stream = None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = open(self.path, 'a+b')
        try:
            if not self.path.stat().st_size:
                stream.write(b'0')
                stream.flush()
            stream.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            stream.close()
            raise RuntimeError('这份数据已有正在运行的 Media Squirrel，请打开现有实例或先正常退出。') from error
        self.stream = stream
        return self

    def release(self):
        if self.stream:
            try:
                self.stream.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
            finally:
                self.stream.close()
                self.stream = None
