"""A cached, locally compiled process identity for source-mode Windows runs.

No installer, bundled Python or downloaded executable. The existing interpreter
and virtual environment remain authoritative. Console/debug modes are unchanged.
"""
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import threading

_BUILD_LOCK = threading.Lock()
IS_WINDOWS = os.name == 'nt'


def host_inputs():
    if not IS_WINDOWS or getattr(sys, 'frozen', False):
        raise RuntimeError('独立进程名称仅支持 Windows 源码运行')
    architecture = platform.machine().lower()
    if architecture not in ('amd64', 'x86_64', 'x86', 'i386', 'i686'):
        raise RuntimeError('此架构暂不支持本地进程宿主，继续使用原 Python 启动')
    bits = 64 if sys.maxsize > 2**32 else 32
    base = Path(sys.base_prefix)
    python = Path(sys.executable)
    dll = base / f'python{sys.version_info.major}{sys.version_info.minor}{getattr(sys, "abiflags", "")}.dll'
    compiler = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Microsoft.NET' / (
        'Framework64' if bits == 64 else 'Framework') / 'v4.0.30319' / 'csc.exe'
    if not python.is_file() or not dll.is_file() or not compiler.is_file():
        raise RuntimeError('缺少本地 Python DLL 或 Windows C# 编译器，继续使用原 Python 启动')
    return python.resolve(), dll.resolve(), compiler, 'x64' if bits == 64 else 'x86'


def host_command(host, python, dll, arguments):
    return [str(host), '--python', str(python), '--dll', str(dll), '--', *arguments]


def ensure_host():
    from . import config
    from PIL import Image
    python, dll, compiler, architecture = host_inputs()
    source = Path(__file__).with_suffix('.cs')
    artwork = Path(__file__).parent / 'assets' / 'squirrel-tray.png'
    fingerprint = hashlib.sha256(source.read_bytes() + artwork.read_bytes() +
        json.dumps([str(python), str(dll), sys.version, architecture, str(Path(sys.prefix).resolve())]).encode()).hexdigest()[:20]
    folder = Path(config.CACHE_DIR) / 'windows-host' / fingerprint
    host = folder / 'MediaSquirrel.exe'
    marker = folder / 'verified.json'
    with _BUILD_LOCK:
        if host.is_file() and marker.is_file():
            try:
                expected = json.loads(marker.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                expected = {}
            if not isinstance(expected, dict):
                expected = {}
            if expected.get('sha256') == hashlib.sha256(host.read_bytes()).hexdigest():
                return host, python, dll
        folder.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='build-', dir=folder) as temporary:
            temp = Path(temporary)
            icon, output, probe = temp / 'squirrel.ico', temp / 'MediaSquirrel.exe', temp / 'probe.json'
            with Image.open(artwork) as image:
                image.save(icon, format='ICO', sizes=[(16, 16), (32, 32), (48, 48),
                                                    (64, 64), (128, 128), (256, 256)])
            result = subprocess.run([str(compiler), '/nologo', '/target:winexe', '/optimize+',
                '/platform:' + architecture, '/win32icon:' + str(icon), '/out:' + str(output), str(source)],
                capture_output=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
            if result.returncode or not output.is_file():
                raise RuntimeError('本地进程宿主生成失败，继续使用原 Python 启动')
            # Verify interpreter/venv identity before making this a startup path.
            script = ('import json,sys,pathlib; pathlib.Path(sys.argv[1]).write_text('
                      'json.dumps({"prefix":sys.prefix,"executable":sys.executable}),encoding="utf-8")')
            result = subprocess.run(host_command(output, python, dll, ['-I', '-c', script, str(probe)]),
                capture_output=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
            if result.returncode or not probe.is_file():
                raise RuntimeError('本地进程宿主无法载入 Python，继续使用原方式启动')
            details = json.loads(probe.read_text(encoding='utf-8'))
            if (Path(details['prefix']).resolve() != Path(sys.prefix).resolve() or
                    Path(details['executable']).resolve() != python):
                raise RuntimeError('进程宿主的 Python 环境不一致，继续使用原方式启动')
            os.replace(output, host)
            from .archive import atomic_write_text
            atomic_write_text(marker, json.dumps({'sha256': hashlib.sha256(host.read_bytes()).hexdigest()}))
    return host, python, dll


def relaunch_if_needed(arguments):
    """Return true only after a branded child has been launched successfully."""
    if (not IS_WINDOWS or getattr(sys, 'frozen', False) or
            os.environ.get('MS_BRANDED_HOST') == str(os.getpid())):
        return False
    from . import config
    try:
        host, python, dll = ensure_host()
        process = subprocess.Popen(host_command(host, python, dll,
            [str(Path(config.SOURCE_DIR) / 'run.py'), *arguments]), cwd=config.SOURCE_DIR,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW, close_fds=True)
        try:
            code = process.wait(timeout=.5)
        except subprocess.TimeoutExpired:
            return True
        if code == 0:
            return True  # Existing matching service may have been reused.
        raise RuntimeError('独立进程启动失败，继续使用原 Python 启动')
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        from .redaction import redact_text
        print('[startup] ' + redact_text(str(error)))
        return False
