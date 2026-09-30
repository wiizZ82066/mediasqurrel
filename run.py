"""一键启动器：启动 FastAPI 并自动打开浏览器。

用法:
    python run.py            # 默认 http://127.0.0.1:8642
    python run.py --no-browser
"""
import argparse
import socket
import threading
import webbrowser

import uvicorn

from app import config


def _wait_and_open(url: str, delay: float = 1.5):
    """等服务端口就绪后打开浏览器。"""
    def _worker():
        host, port = config.HOST, config.PORT
        for _ in range(60):
            try:
                with socket.create_connection((host, port), timeout=0.5):
                    break
            except OSError:
                import time
                time.sleep(0.5)
        webbrowser.open(url)

    threading.Thread(target=_worker, daemon=True).start()


def main():
    ap = argparse.ArgumentParser(description="Media Squirrel 启动器")
    ap.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    ap.add_argument("--port", type=int, default=config.PORT, help=f"端口（默认 {config.PORT}）")
    ap.add_argument("--reload", action="store_true", help="开发模式热重载")
    args = ap.parse_args()

    config.PORT = args.port
    url = f"http://{config.HOST}:{args.port}"
    print(f"[*] Media Squirrel 启动中: {url}")

    if not args.no_browser:
        _wait_and_open(url)

    uvicorn.run(
        "app.main:app",
        host=config.HOST,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )


if __name__ == "__main__":
    main()
