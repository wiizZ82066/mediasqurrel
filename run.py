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
import threading
import webbrowser

import uvicorn

from app import config


def _wait_and_open(url: str):
    """等服务端口就绪后打开浏览器。"""
    def _worker():
        for _ in range(60):
            try:
                with socket.create_connection((config.HOST, config.PORT), timeout=0.5):
                    break
            except OSError:
                import time
                time.sleep(0.5)
        webbrowser.open(url)

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


def _run_tray(url: str):
    """系统托盘常驻：菜单可打开页面 / 退出。"""
    import pystray
    from pystray import Menu, MenuItem

    def open_page(icon, item):
        webbrowser.open(url)

    def quit_app(icon, item):
        icon.stop()
        os._exit(0)  # uvicorn 线程为 daemon，直接退出

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
    icon.run()


def main():
    ap = argparse.ArgumentParser(description="Media Squirrel 启动器")
    ap.add_argument("--no-tray", action="store_true", help="不启用系统托盘（纯控制台）")
    ap.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    ap.add_argument("--port", type=int, default=config.PORT, help=f"端口（默认 {config.PORT}）")
    ap.add_argument("--reload", action="store_true", help="开发模式热重载")
    args = ap.parse_args()

    config.PORT = args.port
    url = f"http://{config.HOST}:{args.port}"
    print(f"[*] Media Squirrel 启动中: {url}")

    if not args.no_browser:
        _wait_and_open(url)

    if args.no_tray:
        uvicorn.run(
            "app.main:app",
            host=config.HOST,
            port=args.port,
            reload=args.reload,
            log_level="info",
        )
    else:
        # uvicorn 在后台线程，托盘占据主线程（Windows 要求）
        server = threading.Thread(
            target=lambda: uvicorn.run(
                "app.main:app",
                host=config.HOST,
                port=args.port,
                reload=False,
                log_level="info",
            ),
            daemon=True,
        )
        server.start()
        try:
            _run_tray(url)
        except Exception as e:
            # 无显示环境等托盘不可用时，退回控制台模式
            print(f"[!] 托盘不可用({e})，回退控制台模式，Ctrl+C 退出")
            server.join()


if __name__ == "__main__":
    main()
