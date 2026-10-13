"""Opt-in Catbus subprocess bridge for read-only Douyin item details.

No Catbus code, dependencies or credentials are bundled. The operator supplies
an installed CLI and its own CATBUS_HOME; browser sessions are never imported.
The inspected CLI protocol is `item get --raw -o json` (catbus e637cb4).
Catbus itself can refresh its credentials and retry a missing UIFID once; this
bridge never retries or switches transport after authentication/risk failures.
"""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import threading
import time

from .platform_errors import PlatformAccessError


ENV_KEYS = ('MS_DOUYIN_ADAPTER_NODE', 'MS_DOUYIN_ADAPTER_ENTRY', 'MS_DOUYIN_ADAPTER_HOME')
WINDOWS_ADAPTER = os.name == 'nt'
SUPPORTED_ENGINES = '^22.22.2 || ^24.15.0 || >=26.0.0'
STDOUT_LIMIT = 8 * 1024 * 1024
STDERR_LIMIT = 64 * 1024
_ID = re.compile(r'[0-9]{5,30}\Z')
_NODE_VERSION = re.compile(rb'v(\d+)\.(\d+)\.(\d+)\s*\Z')


class AdapterCancelled(RuntimeError):
    """Caller cancellation, separate from transport/platform failure."""


def _error(message, reason):
    return PlatformAccessError(message, reason=reason, platform='douyin')


@dataclass(frozen=True)
class _Configuration:
    node: Path
    entry: Path
    home: Path
    node_version: str


def _check_cancelled(cancelled):
    if cancelled is not None and cancelled():
        raise AdapterCancelled('抖音详情解析已取消')


def _stop_process(proc):
    # Each call owns a new process group/session. Never target unrelated PIDs.
    if proc.poll() is None:
        if os.name == 'nt':
            try:
                subprocess.run(['taskkill', '/PID', str(proc.pid), '/T', '/F'],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=3,
                               creationflags=subprocess.CREATE_NO_WINDOW, check=False)
            except (OSError, subprocess.TimeoutExpired):
                pass
        else:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
        if proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        pass


def _run_bounded(argv, *, env, timeout, cancelled=None,
                 stdout_limit=STDOUT_LIMIT, stderr_limit=STDERR_LIMIT):
    """Drain both pipes concurrently, retaining at most the declared limits."""
    _check_cancelled(cancelled)
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=env, shell=False,
                                start_new_session=os.name != 'nt',
                                creationflags=(subprocess.CREATE_NO_WINDOW |
                                               subprocess.CREATE_NEW_PROCESS_GROUP)
                                if os.name == 'nt' else 0)
    except OSError:
        raise _error('无法启动抖音详情适配器，请检查本地安装', 'unavailable') from None
    buffers = [bytearray(), bytearray()]
    overflow = threading.Event()
    read_failed = threading.Event()

    def drain(pipe, buffer, limit):
        try:
            while True:
                chunk = pipe.read(8192)
                if not chunk:
                    break
                remaining = max(0, limit - len(buffer))
                buffer.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    overflow.set()
        except (OSError, ValueError):
            read_failed.set()
        finally:
            pipe.close()

    threads = [threading.Thread(target=drain, args=args, daemon=True)
               for args in ((proc.stdout, buffers[0], stdout_limit),
                            (proc.stderr, buffers[1], stderr_limit))]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + timeout
    try:
        while proc.poll() is None or any(thread.is_alive() for thread in threads):
            _check_cancelled(cancelled)
            if overflow.is_set():
                raise _error('抖音详情适配器响应过大，已停止解析', 'invalid_response')
            if time.monotonic() >= deadline:
                raise _error('抖音详情适配器超时，已停止；请稍后重试', 'timeout')
            time.sleep(0.02)
        _check_cancelled(cancelled)
        if overflow.is_set() or read_failed.is_set():
            raise _error('抖音详情适配器响应不完整，已停止解析', 'invalid_response')
        return proc.returncode, bytes(buffers[0]), bytes(buffers[1])
    finally:
        _stop_process(proc)
        for thread in threads:
            thread.join(timeout=1)


def _child_env(env, home):
    child = dict(env)
    child['CATBUS_HOME'] = str(home)
    # A user-selected Node installation is enough; do not inherit injected JS.
    child.pop('NODE_OPTIONS', None)
    child.pop('NODE_PATH', None)
    return child


def _configuration(env, cancelled=None):
    # Task cancellation currently guarantees descendant cleanup on Windows.
    # Keep other hosts on the browser transport until their CLI lifecycle has
    # a verified parent-exit/cancellation contract as well.
    if not WINDOWS_ADAPTER:
        raise _error('实验性抖音详情适配器目前仅支持 Windows，请使用浏览器解析', 'unavailable')
    paths = []
    for key in ENV_KEYS:
        value = env.get(key, '')
        if not isinstance(value, str) or not value.strip():
            raise _error('未配置独立的 Node、Catbus 入口和数据目录', 'unavailable')
        path = Path(value.strip())
        if not path.is_absolute():
            raise _error('抖音详情适配器路径必须为绝对路径', 'unavailable')
        try:
            paths.append(path.resolve(strict=True))
        except (OSError, ValueError):
            raise _error('抖音详情适配器路径不存在或不可访问', 'unavailable') from None
    node, entry, home = paths
    if node.name.lower() not in ('node', 'node.exe') or not node.is_file():
        raise _error('请配置有效的 Node 可执行文件', 'unavailable')
    if entry.suffix.lower() not in ('.js', '.mjs') or not entry.is_file() or not home.is_dir():
        raise _error('请配置已安装的 Catbus 入口及独立数据目录', 'unavailable')
    package = None
    for parent in list(entry.parents)[:5]:
        manifest = parent / 'package.json'
        if not manifest.is_file():
            continue
        try:
            with manifest.open('rb') as source:
                content = source.read(65537)
            if len(content) > 65536:
                continue
            candidate = json.loads(content)
            if isinstance(candidate, dict) and candidate.get('name') == 'catbus-cli':
                binary = candidate.get('bin', {})
                target = binary.get('catbus') if isinstance(binary, dict) else None
                if isinstance(target, str) and (parent / target).resolve() == entry:
                    package = candidate
                    break
        except (OSError, ValueError, RecursionError):
            continue
    if package is None:
        raise _error('配置的入口不是已安装 Catbus 的公开命令入口', 'unavailable')
    engines = package.get('engines')
    if not isinstance(engines, dict) or engines.get('node') != SUPPORTED_ENGINES:
        raise _error('该 Catbus 版本的 Node 兼容范围尚未适配', 'unavailable')
    try:
        code, stdout, _ = _run_bounded([str(node), '--version'],
                                      env=_child_env(env, home), timeout=3,
                                      cancelled=cancelled, stdout_limit=1024,
                                      stderr_limit=4096)
    except PlatformAccessError:
        raise _error('无法确认 Node 版本，抖音详情适配器暂不可用', 'unavailable') from None
    match = _NODE_VERSION.fullmatch(stdout.strip()) if code == 0 else None
    version = tuple(map(int, match.groups())) if match else (0, 0, 0)
    if not ((version[0] == 22 and version >= (22, 22, 2)) or
            (version[0] == 24 and version >= (24, 15, 0)) or version >= (26, 0, 0)):
        raise _error('Node 版本不满足 Catbus 要求：22.22.2+（22系）、24.15.0+（24系）或26+',
                     'unavailable')
    return _Configuration(node, entry, home, '.'.join(map(str, version)))


def configuration_status(*, env=None):
    """Local-only readiness; never start Catbus, read credentials or go online.

    Node --version is bounded to three seconds; callers on an event loop should
    run this check in a worker. `available` does not assert login/API usability.
    """
    env = dict(os.environ if env is None else env)
    configured = any(bool(env.get(key)) for key in ENV_KEYS)
    try:
        config = _configuration(env)
    except PlatformAccessError as exc:
        return {'configured': configured, 'available': False,
                'reason': exc.reason, 'message': str(exc)}
    return {'configured': True, 'available': True, 'reason': None,
            'message': '本地适配器检查通过；尚未校验独立登录态或联网解析',
            'node_version': config.node_version}


_ERRORS = {
    'AUTH_REQUIRED': ('login_required', '抖音详情适配器需要登录，请先完成其独立登录'),
    'AUTH_EXPIRED': ('login_required', '抖音详情适配器登录已过期，请重新登录'),
    'RISK_CONTROL': ('risk_control', '抖音详情接口触发平台验证，已停止；请稍后处理'),
    'NETWORK': ('network_error', '抖音详情接口网络异常，已停止；请稍后重试'),
    'UNSUPPORTED': ('unsupported', '抖音详情适配器明确不支持此读取能力'),
    'NOT_IMPLEMENTED': ('unsupported', '抖音详情适配器尚未实现此读取能力'),
    'UPSTREAM': ('platform_error', '抖音未返回可用作品详情，已停止解析'),
    'USAGE': ('invalid_response', '抖音详情适配器命令协议不兼容，已停止解析'),
}


def _parse_response(stdout, exit_code, aweme_id):
    # Even a broken envelope must not turn auth/risk exit codes into fallback.
    if exit_code in (3, 5):
        reason, message = _ERRORS['AUTH_REQUIRED' if exit_code == 3 else 'RISK_CONTROL']
        raise _error(message, reason)
    try:
        result = json.loads(stdout)
    except (ValueError, UnicodeError, RecursionError):
        raise _error('抖音详情适配器未返回有效 JSON，已停止解析', 'invalid_response') from None
    if not isinstance(result, dict):
        raise _error('抖音详情适配器返回格式不兼容，已停止解析', 'invalid_response')
    if any(result.get(key) != value for key, value in
           (('platform', 'douyin'), ('endpoint', 'web'), ('resource', 'item'), ('action', 'get'))):
        raise _error('抖音详情适配器返回协议不匹配，已停止解析', 'invalid_response')
    error = result.get('error')
    if result.get('ok') is False and isinstance(error, dict):
        code = error.get('code') if isinstance(error.get('code'), str) else ''
        # Inconsistent failures are not proof that the capability is absent.
        if (code == 'UNSUPPORTED' and exit_code != 2) or (code == 'NOT_IMPLEMENTED' and exit_code != 4):
            raise _error('抖音详情适配器错误协议不匹配，已停止解析', 'invalid_response')
        reason, message = _ERRORS.get(code,
                                      ('platform_error', '抖音详情适配器读取失败，已停止解析'))
        raise _error(message, reason)
    if exit_code != 0 or result.get('ok') is not True or error is not None:
        raise _error('抖音详情适配器返回协议不匹配，已停止解析', 'invalid_response')
    detail = result.get('data')
    if not isinstance(detail, dict) or not isinstance(detail.get('aweme_id'), str) or detail['aweme_id'] != aweme_id:
        raise _error('抖音详情适配器作品编号不匹配，已停止解析', 'invalid_response')
    if not isinstance(detail.get('author'), dict) or not (
            isinstance(detail.get('video'), dict) or isinstance(detail.get('images'), list)):
        raise _error('抖音详情适配器作品资料不完整，已停止解析', 'invalid_response')
    return detail


def fetch_detail(aweme_id, *, env=None, timeout=45.0, cancelled=None):
    """Return one raw aweme; downloading/archiving remains in MediaSquirrel.

    Only explicit local unavailability or CLI unsupported errors allow callers
    to fall back once. This function neither retries nor invokes a browser.
    """
    if not isinstance(aweme_id, str) or not _ID.fullmatch(aweme_id):
        raise _error('抖音作品编号无效', 'invalid_response')
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 120:
        raise ValueError('抖音详情解析超时必须在 0 到 120 秒之间')
    env = dict(os.environ if env is None else env)
    config = _configuration(env, cancelled)
    code, stdout, _stderr = _run_bounded(
        [str(config.node), str(config.entry), 'douyin', 'item', 'get', aweme_id,
         '-e', 'web', '-o', 'json', '--raw', '--quiet'],
        env=_child_env(env, config.home), timeout=timeout, cancelled=cancelled)
    return _parse_response(stdout, code, aweme_id)
