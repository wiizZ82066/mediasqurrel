"""一键启动器：启动 FastAPI + 系统托盘常驻 + 自动打开浏览器。

用法:
    python run.py                 # 托盘常驻模式（默认），关闭窗口后仍在后台运行
    python run.py --no-tray       # 无托盘，纯控制台服务
    python run.py --no-browser    # 不自动打开浏览器
    python run.py --port 9000     # 自定义端口
"""
import argparse
import os
import socket
import subprocess
import sys
import threading
import webbrowser


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

import uvicorn

from app import config


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


def _wait_and_open(url: str):
    """等服务端口就绪后打开浏览器。"""
    def _worker():
        for _ in range(60):
            try:
                from app.runtime import probe_server, instance_id
                health = probe_server(config.PORT)
                if health and health.get('instance_id') == instance_id():
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
    """生成托盘图标（松鼠棕圆点 + 白色 S）。"""
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((4, 4, 60, 60), fill=(140, 95, 60, 255))       # 松鼠棕
    d.ellipse((14, 14, 50, 50), outline=(255, 255, 255, 230), width=3)
    d.text((26, 20), "S", fill=(255, 255, 255, 255))
    return img


def _watch_backend(worker, icon, stopped):
    """Stop the tray after normal shutdown, watchdog shutdown or backend failure."""
    while not stopped.is_set():
        worker.join(timeout=.25)
        if not worker.is_alive():
            if not stopped.is_set():
                icon.stop()
            return


def _run_tray(url: str, server, worker):
    """系统托盘常驻：菜单可打开页面 / 退出。"""
    import pystray
    from pystray import Menu, MenuItem

    def open_page(icon, item):
        webbrowser.open(url)

    def quit_app(icon, item):
        server.should_exit = True
        icon.stop()

    icon = pystray.Icon(
        "media_squirrel",
        icon=_make_tray_icon(),
        title=f"Media Squirrel — {url}",
        menu=Menu(
            MenuItem(f"打开页面 ({url})", open_page, default=True),
            Menu.SEPARATOR,
            MenuItem("退出", quit_app),
        ),
    )
    stopped = threading.Event()
    monitor = None

    def ready(tray):
        nonlocal monitor
        tray.visible = True
        monitor = threading.Thread(target=_watch_backend, args=(worker, tray, stopped),
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
    args = ap.parse_args()

    if not 1024 <= args.port <= 65535:
        ap.error('端口必须在 1024–65535 之间')
    config.PORT = args.port
    os.environ['MS_PORT'] = str(args.port)
    url = f"http://{config.HOST}:{args.port}"
    from app.runtime import ensure_frontend, probe_server, port_in_use, instance_id
    existing = probe_server(args.port)
    if existing:
        if existing.get('instance_id') != instance_id():
            raise SystemExit('此端口的 Media Squirrel 正在使用另一份数据，请先退出旧实例或选择其他端口。')
        print(f'[*] Media Squirrel 已在运行：{url}')
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
        _wait_and_open(url)
    if args.reload:
        uvicorn.run('app.main:app', host=config.HOST, port=args.port, reload=True, log_level='info', timeout_graceful_shutdown=10)
        return
    service = uvicorn.Server(uvicorn.Config('app.main:app', host=config.HOST, port=args.port, log_level='info', timeout_graceful_shutdown=10))
    if os.environ.get('MS_DESKTOP_TOKEN') or getattr(sys, 'frozen', False):
        _stdin_watchdog(lambda: setattr(service, 'should_exit', True))
    if args.no_tray:
        service.run()
        return
    worker = threading.Thread(target=service.run, name='media-squirrel-server')
    worker.start()
    try:
        import time
        for _ in range(200):
            if service.started or not worker.is_alive():
                break
            time.sleep(.1)
        if not worker.is_alive():
            raise SystemExit('本地服务启动失败，请检查上方错误。')
        try:
            _run_tray(url, service, worker)
        except Exception as error:
            print(f'[!] 托盘不可用({error})，当前使用控制台模式，Ctrl+C 退出。')
            worker.join()
    except KeyboardInterrupt:
        pass
    finally:
        service.should_exit = True
        worker.join()


if __name__ == "__main__":
    main()
