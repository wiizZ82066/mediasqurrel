"""一键启动器：启动 FastAPI + 系统托盘常驻 + 自动打开浏览器。

用法:
    python run.py                 # 托盘常驻模式（默认），关闭窗口后仍在后台运行
    python run.py --no-tray       # 无托盘，纯控制台服务
    python run.py --no-browser    # 不自动打开浏览器
    python run.py --port 9000     # 自定义端口
    python run.py --background    # Windows 隐藏控制台，在托盘后台运行
"""
import argparse
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import webbrowser


_BACKGROUND = __name__ == '__main__' and os.name == 'nt' and (
    '--background-child' in sys.argv or Path(sys.executable).name.lower() == 'pythonw.exe')
_STARTUP_LOG = None
_LOG_STREAM = None
_LOG_HANDLER = None


def _report_startup_error(error):
    """A windowless launch must never fail silently or expose credentials."""
    from app.windows_runtime import message_box
    from app.redaction import redact_text
    reason = redact_text(str(error))
    print('[startup] ' + reason, file=sys.stderr)
    if _BACKGROUND:
        details = f'\n\n启动日志：{_STARTUP_LOG}' if _STARTUP_LOG else '\n请用 python run.py 在控制台查看错误。'
        message_box(reason + details)


def _prepare_background():
    global _STARTUP_LOG, _LOG_STREAM, _LOG_HANDLER
    # HKCU Run has no working-directory field; source paths must be stable even
    # when Windows starts us from System32. Never change a caller's console cwd.
    os.chdir(Path(__file__).resolve().parent)
    if '--paths-file' in sys.argv:
        index = sys.argv.index('--paths-file')
        if index + 1 >= len(sys.argv) or not os.path.isabs(sys.argv[index + 1]):
            raise ValueError('--paths-file 需要绝对路径')
        os.environ['MS_PATHS_FILE'] = sys.argv[index + 1]
    from app.windows_runtime import configure_background_logging
    try:
        from app import config as launch_config
        directory = Path(launch_config.LOG_DIR) / 'startup'
    except Exception:
        directory = Path(os.environ.get('LOCALAPPDATA') or Path.home()) / 'Media Squirrel' / 'startup-logs'
    try:
        _STARTUP_LOG, _LOG_STREAM, _LOG_HANDLER = configure_background_logging(directory)
    except OSError:
        fallback = Path(os.environ.get('TEMP') or Path.home()) / 'MediaSquirrel-startup-logs'
        _STARTUP_LOG, _LOG_STREAM, _LOG_HANDLER = configure_background_logging(fallback)
    sys.stdout = sys.stderr = _LOG_STREAM
    # pythonw may have None for all three standard streams.
    if sys.stdin is None:
        sys.stdin = open(os.devnull, 'r', encoding='utf-8')


if _BACKGROUND:
    try:
        _prepare_background()
    except Exception as _error:
        _report_startup_error(_error)
        raise SystemExit(1)


# Electron and the task manager consume UTF-8 pipes. Frozen Python ignores
# PYTHONIOENCODING/PYTHONUTF8, so configure output before any Chinese log line,
# including the --internal-run downloader path on non-Chinese Windows.
for _output in (sys.stdout, sys.stderr):
    if hasattr(_output, "reconfigure"):
        _output.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)


def _handle_internal_run() -> bool:
    """PyInstaller frozen 模式的子进程路由。

    打包后 sys.executable 指向自身 exe，无法 `python xxx.py`。
    task_manager 会以 `<exe> --internal-run <script> [args...]` 启动下载脚本，
    这里用 runpy 在同一 bundle 环境内执行脚本源文件。
    """
    if "--internal-run" not in sys.argv:
        return False
    idx = sys.argv.index("--internal-run")
    if idx + 1 >= len(sys.argv):
        print("用法: <exe> --internal-run <script.py> [args...]")
        sys.exit(1)
    script = os.path.abspath(sys.argv[idx + 1])
    sys.argv = [script] + sys.argv[idx + 2:]
    import runpy
    runpy.run_path(script, run_name="__main__")
    sys.exit(0)


_handle_internal_run()

try:
    import uvicorn
    from app import config
except Exception as _error:
    if _BACKGROUND:
        _report_startup_error(_error)
        raise SystemExit(1)
    raise


def _stdin_watchdog(on_exit=None):
    """桌面模式看门狗：Electron 主进程被强杀时（before-quit 不触发），
    本进程 stdin 收到 EOF → 自杀，避免后端残留后台。

    stdin 连接父进程管道时启用；终端直跑时不启用。
    --no-watchdog 可显式关闭。
    """
    if "--no-watchdog" in sys.argv or not sys.stdin or sys.stdin.isatty():
        return

    if os.name == "nt":
        import ctypes
        import msvcrt
        import time
        from ctypes import wintypes

        # An indefinitely blocked stdin read can stall NumPy DLL loading and
        # subsequent API worker startup on Windows. Poll for data/disconnect
        # instead; only read bytes already available in the parent pipe.
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        peek_pipe = kernel.PeekNamedPipe
        peek_pipe.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                              wintypes.LPVOID, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
        peek_pipe.restype = wintypes.BOOL
        read_file = kernel.ReadFile
        read_file.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                              ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
        read_file.restype = wintypes.BOOL
        handle = msvcrt.get_osfhandle(sys.stdin.fileno())
        buffer = ctypes.create_string_buffer(1024)
        available = wintypes.DWORD()
        count = wintypes.DWORD()

        def read_parent():
            if not peek_pipe(handle, None, 0, None, ctypes.byref(available), None):
                return False
            if available.value:
                return read_file(handle, buffer, min(available.value, len(buffer)),
                                 ctypes.byref(count), None) and count.value
            time.sleep(0.25)
            return True
    else:
        def read_parent():
            return sys.stdin.read(1)

    def _watch():
        try:
            while read_parent():
                pass
        except Exception:
            pass
        # EOF：父进程已退出
        if on_exit is not None:
            on_exit()
        else:
            os._exit(0)  # Legacy direct watchdog caller without a server handle.

    threading.Thread(target=_watch, daemon=True).start()


def _wait_and_open(url: str, expected_identity=None):
    """等本次代码与数据对应的服务就绪后打开浏览器。"""
    from app.runtime import existing_instance_problem, probe_server, runtime_identity
    expected = dict(expected_identity or runtime_identity())
    def _worker():
        for _ in range(60):
            try:
                health = probe_server(config.PORT)
                if health:
                    problem = existing_instance_problem(health, expected)
                    if problem:
                        print('[!] ' + problem)
                        return
                    webbrowser.open(url)
                    return
            except OSError:
                import time
                time.sleep(0.5)
            import time
            time.sleep(0.5)
        print('[!] 服务尚未就绪；修复控制台中的错误后再打开页面。')

    threading.Thread(target=_worker, daemon=True).start()


def _make_tray_icon():
    """Use the same squirrel artwork as the application and README."""
    from app.tray_menu import load_brand_icon
    return load_brand_icon()


def _watch_backend(worker, icon, stopped, refresh=None):
    """Stop the tray after normal shutdown, watchdog shutdown or backend failure."""
    count = 0
    while not stopped.is_set():
        worker.join(timeout=.25)
        if not worker.is_alive():
            if not stopped.is_set():
                icon.stop()
            return
        count += 1
        if refresh and count % 8 == 0:
            try:
                refresh()
            except Exception:
                pass


def _tray_activity():
    from app import task_manager, watcher, live_service
    # Copy the in-memory dictionaries before iterating on the tray thread.
    tasks = list(task_manager.TASKS.values())
    return {'running': sum(task.get('status') == 'running' for task in tasks),
            'queued': sum(task.get('status') == 'queued' for task in tasks),
            'scanning': len(watcher._SCANNING), 'recording': live_service.active_count()}


def _tray_status(server):
    if server.should_exit:
        return '正在退出并清理子进程…'
    if not server.started:
        return '正在启动本地服务…'
    state = _tray_activity()
    if not any(state.values()):
        return '服务运行中 · 当前空闲'
    return f"下载 {state['running']} · 排队 {state['queued']} · 扫描 {state['scanning']} · 录播 {state.get('recording', 0)}"


def _request_tray_exit(server, icon):
    from app.windows_runtime import message_box
    if server.should_exit:
        return
    state = _tray_activity()
    if any(state.values()) and not message_box(
            f"当前有 {state['running']} 个下载、{state['queued']} 个排队任务和 {state['scanning']} 个扫描、{state.get('recording', 0)} 路录播。\n"
            '退出会中断正在运行的工作；排队任务下次启动继续，已中断的下载需重试，录播会保存已收到的部分。确定退出？', question=True):
        return
    server.should_exit = True
    icon.title = 'Media Squirrel — 正在退出并清理子进程'
    if os.name != 'nt':
        icon.update_menu()


def _tray_exit_handler(server):
    """Keep native modal waits out of pystray's notification callback.

    A busy confirmation can stay open indefinitely while the tray thread keeps
    pumping messages. Only one prompt may be open, and No leaves work running.
    """
    pending = threading.Lock()

    def request(icon):
        if server.should_exit or not pending.acquire(blocking=False):
            return

        def confirm():
            try:
                _request_tray_exit(server, icon)
            except Exception as error:
                from app.redaction import redact_text
                print('[tray] 无法完成退出请求：' + redact_text(str(error)))
            finally:
                pending.release()

        try:
            threading.Thread(target=confirm, name='media-squirrel-exit-confirm', daemon=True).start()
        except BaseException:
            pending.release()
            raise

    return request


def _run_tray(url: str, server, worker):
    """Keep the tray present until normal backend/child cleanup finishes."""
    from app.tray_menu import build_menu
    from app.tray_windows import create_icon
    from app.windows_runtime import message_box, open_directory

    def open_page(path):
        webbrowser.open(url.rstrip('/') + path)

    quit_app = _tray_exit_handler(server)

    def open_folder(target):
        try:
            open_directory(target)
        except (OSError, RuntimeError, ValueError) as error:
            print('[tray] ' + str(error))
            message_box(str(error))

    def status_text(item=None):
        return _tray_status(server)

    icon = create_icon(
        "media_squirrel",
        icon=_make_tray_icon(),
        title=f"Media Squirrel — {url}",
        menu=build_menu(status_text, open_page, open_folder, quit_app,
                        enabled=lambda item: not server.should_exit),
    )
    stopped = threading.Event()
    monitor = None

    def ready(tray):
        nonlocal monitor
        tray.visible = True
        print('[startup] 托盘已就绪')
        def refresh():
            title = 'Media Squirrel — ' + status_text()
            if tray.title != title:
                tray.title = title
                # Windows refreshes on its own UI thread before opening the
                # popup. Rebuilding an open menu from this monitor can race.
                if os.name != 'nt':
                    tray.update_menu()
        refresh()
        monitor = threading.Thread(target=_watch_backend, args=(worker, tray, stopped, refresh),
                                   name='media-squirrel-tray-watch', daemon=True)
        monitor.start()

    try:
        icon.run(setup=ready)
    finally:
        stopped.set()
        if monitor:
            monitor.join(timeout=1)


def main():
    ap = argparse.ArgumentParser(description="Media Squirrel 启动器")
    ap.add_argument("--no-tray", action="store_true", help="不启用系统托盘（纯控制台）")
    ap.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    ap.add_argument("--no-watchdog", action="store_true", help="禁用 stdin 看门狗（桌面模式父进程存活检测）")
    ap.add_argument("--port", type=int, default=config.PORT, help=f"端口（默认 {config.PORT}）")
    ap.add_argument("--reload", action="store_true", help="开发模式热重载")
    ap.add_argument('--background', action='store_true', help='Windows 隐藏控制台并保留托盘')
    ap.add_argument('--console', action='store_true', help='保留控制台（可用于 start.bat 排错）')
    ap.add_argument('--background-child', action='store_true', help=argparse.SUPPRESS)
    ap.add_argument('--autostart', action='store_true', help=argparse.SUPPRESS)
    ap.add_argument('--paths-file', help=argparse.SUPPRESS)
    args = ap.parse_args()
    if args.autostart:
        print('[startup] 启动方式：Windows 自启动入口')
    if _BACKGROUND:
        print(f'[startup] 进程 PID={os.getpid()}，父进程 PID={os.getppid()}；'
              f'入口标记={"autostart" if args.autostart else "background"}；端口={args.port}')

    if args.background and not (args.console or args.no_tray or args.reload or _BACKGROUND):
        from app.windows_runtime import launch_background
        try:
            launch_background(sys.argv[1:])
        except (RuntimeError, OSError) as error:
            raise SystemExit(str(error))
        return
    if _BACKGROUND and (args.no_tray or args.reload):
        raise SystemExit('隐藏运行必须保留托盘；调试请使用 python run.py --no-tray 或 --reload')

    if not 1024 <= args.port <= 65535:
        ap.error('端口必须在 1024–65535 之间')
    config.PORT = args.port
    os.environ['MS_PORT'] = str(args.port)
    from app.runtime import coordinate_startup, runtime_identity
    try:
        expected = runtime_identity()
    except (RuntimeError, OSError) as error:
        raise SystemExit(str(error))
    with coordinate_startup(args.port) as existing:
        _serve_or_reopen(args, expected, existing)


def _serve_or_reopen(args, expected, existing):
    from app.runtime import ensure_frontend, port_in_use, existing_instance_problem
    url = f"http://{config.HOST}:{args.port}"
    if existing:
        problem = existing_instance_problem(existing, expected)
        if problem:
            raise SystemExit(problem)
        try:
            build_state = ensure_frontend(build=False)
        except (RuntimeError, OSError) as error:
            raise SystemExit(str(error))
        if build_state not in ('current', 'external'):
            raise SystemExit('当前前端构建缺失、损坏或与源码不一致。请等待任务结束，从系统托盘选择“退出”'
                             '（控制台模式按 Ctrl+C），再重新运行启动器完成构建。关闭网页不会停止服务。')
        print(f'[*] Media Squirrel 已在运行，启动代码与当前源码一致：{url}')
        if not args.no_browser:
            webbrowser.open(url)
        return
    if port_in_use(args.port):
        raise SystemExit(f'端口 {args.port} 被其他程序占用。可使用 python run.py --port 9000；未终止其他程序。')
    try:
        ensure_frontend()
    except (RuntimeError, OSError, subprocess.SubprocessError) as error:
        raise SystemExit(str(error))
    print(f'[*] Media Squirrel 启动中：{url}')
    print('[*] 关闭网页会继续后台运行；从托盘选择退出或按 Ctrl+C 才会停止扫描。')
    if not args.no_browser:
        _wait_and_open(url, expected_identity=expected)
    if args.reload:
        uvicorn.run('app.main:app', host=config.HOST, port=args.port, reload=True, log_level='info', timeout_graceful_shutdown=10)
        return
    service = uvicorn.Server(uvicorn.Config('app.main:app', host=config.HOST, port=args.port, log_level='info', timeout_graceful_shutdown=10))
    if os.environ.get('MS_DESKTOP_TOKEN') or getattr(sys, 'frozen', False):
        _stdin_watchdog(lambda: setattr(service, 'should_exit', True))
    if args.no_tray:
        service.run()
        if not service.started:
            raise SystemExit('本地服务启动失败，请检查上方错误。')
        return
    worker = threading.Thread(target=service.run, name='media-squirrel-server')
    worker.start()
    try:
        import time
        for _ in range(200):
            if service.started or not worker.is_alive():
                break
            time.sleep(.1)
        # A large database migration/recovery can outlast the initial wait.
        # Keep a visible tray with "starting" status while the worker is alive;
        # only an exited worker proves startup failure.
        if not worker.is_alive():
            raise SystemExit('本地服务启动失败，请检查上方错误。')
        try:
            _run_tray(url, service, worker)
        except Exception as error:
            if _BACKGROUND:
                raise RuntimeError('托盘启动失败，后台服务将正常退出。请使用 python run.py 检查 pystray 安装：' + str(error)) from error
            print(f'[!] 托盘不可用({error})，当前使用控制台模式，Ctrl+C 退出。')
            worker.join()
        if not service.should_exit:
            raise SystemExit('后台服务意外停止，请查看启动日志。')
    except KeyboardInterrupt:
        pass
    finally:
        service.should_exit = True
        worker.join()


if __name__ == "__main__":
    try:
        main()
    except SystemExit as _error:
        if _BACKGROUND and _error.code not in (None, 0):
            _report_startup_error(_error)
        raise
    except Exception as _error:
        if _BACKGROUND:
            import traceback
            traceback.print_exc()
            _report_startup_error(_error)
            raise SystemExit(1)
        raise
    finally:
        if _LOG_STREAM:
            _LOG_STREAM.close()
        if _LOG_HANDLER:
            _LOG_HANDLER.close()
