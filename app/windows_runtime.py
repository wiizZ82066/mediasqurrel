"""Current-user Windows integration, with no registry writes on import/startup."""
import datetime as dt
import io
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import subprocess
import sys
import threading

from .redaction import redact_text
from . import startup_shortcut

RUN_KEY = r'Software\Microsoft\Windows\CurrentVersion\Run'
RUN_NAME = 'MediaSquirrel'
PATH_OVERRIDES = ('MS_HOME', 'MS_DATA_DIR', 'MS_APP_DATA_DIR', 'MS_LIBRARY_DIR')
IS_WINDOWS = os.name == 'nt'
_startup_lock = threading.Lock()


def console_python(executable=None):
    """Keep downloader pipes functional when the parent uses pythonw.exe."""
    executable = Path(executable or sys.executable)
    if IS_WINDOWS and executable.name.lower() == 'pythonw.exe':
        console = executable.with_name('python.exe')
        if not console.is_file():
            raise RuntimeError('当前 Python 环境缺少 python.exe，无法读取下载日志')
        return str(console)
    return str(executable)


def windowless_python(executable=None):
    executable = Path(executable or sys.executable)
    if not IS_WINDOWS or getattr(sys, 'frozen', False):
        raise RuntimeError('隐藏后台启动仅支持 Windows Python 源码运行')
    windowless = executable.with_name('pythonw.exe')
    if not windowless.is_file():
        raise RuntimeError('当前 Python 环境缺少 pythonw.exe；请修复 Python 安装，或使用 python run.py')
    return str(windowless)


def launch_background(arguments):
    """Launch the same interpreter environment, never a shell command string."""
    from . import config
    args = [arg for arg in arguments if arg not in ('--background', '--console', '--background-child')]
    return subprocess.Popen([windowless_python(), str(Path(config.SOURCE_DIR) / 'run.py'),
                             '--background-child', '--no-watchdog', *args],
                            cwd=config.SOURCE_DIR, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            creationflags=subprocess.CREATE_NO_WINDOW, close_fds=True)


def _startup_arguments():
    from . import config
    if any(os.environ.get(name) for name in PATH_OVERRIDES):
        raise RuntimeError('当前使用临时路径环境变量；请先改用持久路径配置再启用自启动，避免登录后打开另一份数据')
    source = Path(config.SOURCE_DIR).resolve()
    script = source / 'run.py'
    if not script.is_file():
        raise RuntimeError('源码启动器 run.py 不存在')
    args = [windowless_python(), str(script), '--background-child', '--no-browser', '--no-watchdog']
    if config.PORT != 8642:
        args += ['--port', str(config.PORT)]
    paths_file = Path(config.PATHS_FILE).resolve()
    if paths_file != source / '.mediasquirrel.local.json':
        args += ['--paths-file', str(paths_file)]
    return args


def startup_command():
    """Legacy Run registration; retained for detection and rollback."""
    command = subprocess.list2cmdline(_startup_arguments())
    # HKCU Run documents a 260-character limit; count UTF-16 code units.
    # https://learn.microsoft.com/windows/win32/setupapi/run-and-runonce-registry-keys
    if len(command.encode('utf-16-le')) // 2 > 260:
        raise RuntimeError('自启动命令超过 Windows 的 260 字符限制；请缩短源码、Python 或路径配置的位置')
    return command


def _startup_shortcut():
    from . import config
    args = _startup_arguments()
    return {'target': args[0], 'arguments': subprocess.list2cmdline([*args[1:], '--autostart']),
            'working_directory': str(Path(config.SOURCE_DIR).resolve())}


def _registry():
    import winreg
    return winreg


def _registered_command():
    registry = _registry()
    try:
        with registry.OpenKey(registry.HKEY_CURRENT_USER, RUN_KEY, 0, registry.KEY_READ) as key:
            value, kind = registry.QueryValueEx(key, RUN_NAME)
        return value if kind == registry.REG_SZ and isinstance(value, str) else None
    except FileNotFoundError:
        return None


def startup_status():
    status = {'supported': IS_WINDOWS and not getattr(sys, 'frozen', False), 'available': False,
              'enabled': False, 'matches_current': False, 'method': None,
              'message': '开机自启动仅支持 Windows Python 源码运行'}
    if not status['supported']:
        return status
    try:
        actual = _registered_command()
        status['enabled'] = actual is not None
        link = startup_shortcut.shortcut()
        status['enabled'] = status['enabled'] or link is not None
        status['method'] = 'shortcut' if link else 'legacy_run' if actual is not None else None
        expected = _startup_shortcut()
        current = startup_shortcut.matches(link, expected) and actual is None
        status.update(available=True, matches_current=current)
        status['message'] = ('已核验 Windows 启动快捷方式，登录后在托盘运行，不自动打开网页；可在 Windows 启动应用中管理'
                             if current else
                             '检测到旧版自启动登记或重复入口，请点击“重新登记”更新' if actual is not None else
                             '自启动快捷方式指向旧位置，请点击“重新登记”修复' if link else
                             '默认关闭；开启后在当前用户登录 Windows 时后台运行，不会唤醒电脑')
    except (OSError, RuntimeError) as error:
        status['message'] = redact_text(str(error))
    return status


def _restore_registered_command(command):
    registry = _registry()
    if command is not None:
        with registry.CreateKeyEx(registry.HKEY_CURRENT_USER, RUN_KEY, 0, registry.KEY_SET_VALUE) as key:
            registry.SetValueEx(key, RUN_NAME, 0, registry.REG_SZ, command)
    else:
        try:
            with registry.OpenKey(registry.HKEY_CURRENT_USER, RUN_KEY, 0, registry.KEY_SET_VALUE) as key:
                registry.DeleteValue(key, RUN_NAME)
        except FileNotFoundError:
            pass


def set_startup(enabled):
    if type(enabled) is not bool:
        raise ValueError('enabled 必须是布尔值')
    if not IS_WINDOWS or getattr(sys, 'frozen', False):
        raise RuntimeError('开机自启动仅支持 Windows Python 源码运行')
    with _startup_lock:
        expected = _startup_shortcut() if enabled else None
        previous_link = startup_shortcut.shortcut()
        previous_command = _registered_command()
        try:
            current = startup_shortcut.shortcut('write', value=expected) if enabled else startup_shortcut.shortcut('remove')
            if (current is not None) != enabled or (enabled and not startup_shortcut.matches(current, expected)):
                raise RuntimeError('无法核实自启动快捷方式是否已保存')
            # Replace the legacy entry only after the shortcut has been read back.
            _restore_registered_command(None)
            if _registered_command() is not None:
                raise RuntimeError('无法移除旧自启动登记，已取消修改以避免重复启动')
        except Exception:
            try:
                startup_shortcut.shortcut('restore', content=previous_link['content'] if previous_link else None)
                if _registered_command() != previous_command:
                    _restore_registered_command(previous_command)
            except Exception as rollback_error:
                raise RuntimeError('自启动修改失败且未能完整还原，请重新检查启动状态') from rollback_error
            raise
        return startup_status()


def open_directory(target):
    """Fixed directory allowlist; no arbitrary path/verb/command input."""
    from . import config
    directories = {'library': config.LIBRARY_ROOT, 'data': config.DATA_DIR,
                   'logs': config.LOG_DIR, 'source': config.SOURCE_DIR}
    if target not in directories:
        raise ValueError('未知目录；请选择媒体、数据、日志或项目目录')
    if not IS_WINDOWS:
        raise RuntimeError('资源管理器入口仅支持 Windows')
    path = Path(directories[target]).resolve()
    if not path.is_dir():
        raise FileNotFoundError('目录不存在，请检查路径设置或磁盘连接')
    os.startfile(str(path), 'explore')


def message_box(message, *, question=False):
    if not IS_WINDOWS:
        return not question
    import ctypes
    from ctypes import wintypes
    show = ctypes.WinDLL('user32', use_last_error=True).MessageBoxW
    show.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT]
    show.restype = ctypes.c_int
    # Questions default to No; failures are explicit startup dialogs, not notifications.
    flags = 0x00000004 | 0x00000030 | 0x00000100 if question else 0x00000010
    # The tray calls this on a separate thread so its own message loop stays
    # responsive. Do not attach to pystray's invisible cross-thread window.
    flags |= 0x00010000  # MB_SETFOREGROUND
    result = show(None, redact_text(str(message))[:3500], 'Media Squirrel', flags)
    return result == 6 if question else True


class LogStream(io.TextIOBase):
    """Bounded line buffering keeps split print writes together before redaction."""
    encoding = 'utf-8'
    errors = 'replace'

    def __init__(self, logger):
        super().__init__()
        self.logger = logger
        self.pending = ''
        self.overflow = False
        self.lock = threading.RLock()

    def isatty(self):
        return False

    def write(self, value):
        text = str(value)
        with self.lock:
            for part in text.splitlines(keepends=True):
                end = part.endswith(('\n', '\r'))
                if not self.overflow:
                    self.pending += part
                    if len(self.pending) > 16384:
                        # Drop the rest of an overlong line instead of leaking a
                        # credential whose key was in the previous chunk.
                        self.pending = self.pending[:16384] + ' [日志过长，后续已省略]'
                        self.overflow = True
                if end:
                    self._emit()
        return len(text)

    def _emit(self):
        if self.pending.strip():
            self.logger.info(redact_text(self.pending.rstrip('\r\n')))
        self.pending = ''
        self.overflow = False

    def flush(self):
        # Uvicorn flushes every record. Keep partial print lines until newline;
        # close() is the only place to finalize an incomplete line.
        for handler in self.logger.handlers:
            handler.flush()

    def close(self):
        if not self.closed:
            with self.lock:
                self._emit()
            super().close()


def configure_background_logging(directory):
    """Startup diagnostics are bounded separately from permanent task logs."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    path = directory / f'startup-{stamp}-{os.getpid()}.log'
    handler = RotatingFileHandler(path, maxBytes=1024 * 1024, backupCount=2,
                                  encoding='utf-8', errors='replace')
    handler.setFormatter(logging.Formatter('%(asctime)s %(message)s', datefmt='%Y-%m-%dT%H:%M:%S%z'))
    logger = logging.Logger('media-squirrel-startup', level=logging.INFO)
    logger.addHandler(handler)
    logger.propagate = False
    # Keep the current run and four earlier runs; an open file on Windows is
    # retained when deletion fails, then reconsidered on the next launch.
    previous = sorted(directory.glob('startup-*.log'), key=lambda item: item.name, reverse=True)
    for old in previous[5:]:
        for candidate in (old, old.with_name(old.name + '.1'), old.with_name(old.name + '.2')):
            try:
                candidate.unlink(missing_ok=True)
            except OSError:
                pass
    stream = LogStream(logger)
    return path, stream, handler
