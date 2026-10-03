"""抖音视频下载器（playwright + 系统 Chrome，零 scrapling 依赖）。

用法:
    python douyin_downloader.py <视频URL|v.douyin.com短链|分享文案> [-o 输出根目录]

流程:
    1. 用系统 Chrome 打开视频页，拦截详情接口 aweme/v1/web/aweme/detail
    2. 从返回里取作者昵称、视频 id、无水印 h264 直链
    3. 下载 mp4 到 <输出根目录>/作者昵称/YYYY-MM-DD-HH-MM/视频id.mp4
    4. 在同目录生成 context.md（作者/链接/发布时间/标题/文件名/UID/sec_uid）

-o/--out 不传时沿用旧行为：输出根目录 = 本脚本所在目录。
"""
import json
import os
import re
import sys
import time
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import requests

# 统一浏览器引擎（app/browser.py；脚本独立运行时按相对路径加载）
try:
    from app.browser import XHRHunter, launch_chrome, sync_playwright
except ImportError:
    import importlib.util
    _spec = importlib.util.spec_from_file_location(
        "_ms_browser", os.path.join(os.path.dirname(os.path.abspath(__file__)), "app", "browser.py")
    )
    _mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    XHRHunter, launch_chrome, sync_playwright = _mod.XHRHunter, _mod.launch_chrome, _mod.sync_playwright

BASE = os.path.dirname(os.path.abspath(__file__))

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)


def sanitize(s: str) -> str:
    s = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", str(s)).strip().strip(".")
    return s or "untitled"


_URL_RE = re.compile(r"https?://[A-Za-z0-9./?=&_%:@+~#-]+")


def extract_douyin_url(text: str) -> str | None:
    """从分享文案里提取第一个抖音链接。"""
    for u in _URL_RE.findall(text):
        if "douyin.com" in u or "iesdouyin.com" in u:
            return u.rstrip(".,;:!?。，；：！？")
    return None


def extract_video_id(url: str) -> str | None:
    """从 URL 里提取视频 id：/video/{id} > modal_id > vid。"""
    m = re.search(r"/video/(\d+)", url)
    if m:
        return m.group(1)
    m = re.search(r"[?&]modal_id=(\d+)", url)
    if m:
        return m.group(1)
    m = re.search(r"[?&]vid=(\d+)", url)
    if m:
        return m.group(1)
    return None


def resolve_to_video_url(raw: str) -> str:
    """把分享文案/短链/各种链接统一解析成 https://www.douyin.com/video/{id}。"""
    url = extract_douyin_url(raw)
    if not url:
        raise ValueError("未找到抖音链接")
    vid = extract_video_id(url)
    if vid:
        return f"https://www.douyin.com/video/{vid}"
    # 短链：跟随重定向拿到真实地址
    r = requests.get(url, headers={"User-Agent": UA}, allow_redirects=True, timeout=30)
    vid = extract_video_id(r.url)
    if vid:
        return f"https://www.douyin.com/video/{vid}"
    raise ValueError(
        f"无法从链接解析出视频 id: {r.url}\n"
        "提示: 若链接含 & 参数（modal_id/vid 等），请给整段 URL 加双引号，"
        "否则 cmd/PowerShell 会把 & 当作命令分隔符，把链接截断。"
    )


def fetch_aweme_detail(url: str) -> dict:
    """系统 Chrome 打开视频页，拦截 aweme/v1/web/aweme/detail 响应。"""
    with sync_playwright() as p:
        browser = launch_chrome(p, headless=True)
        try:
            ctx = browser.new_context(user_agent=UA, locale="zh-CN")
            page = ctx.new_page()
            hunter = XHRHunter(r"aweme/v1/web/aweme/detail").attach(page)
            page.goto(url, wait_until="domcontentloaded", timeout=60000,
                      referer="https://www.google.com/")
            page.wait_for_timeout(6000)
        finally:
            browser.close()

    for data in hunter.json_results():
        if isinstance(data, dict) and "aweme_detail" in data:
            return data["aweme_detail"]
    raise RuntimeError("未捕获到 aweme_detail 接口响应")


def download(url: str, dest: str) -> int:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": UA, "Referer": "https://www.douyin.com/"},
    )
    with urllib.request.urlopen(req, timeout=120) as resp, open(dest, "wb") as f:
        while True:
            chunk = resp.read(1 << 16)
            if not chunk:
                break
            f.write(chunk)
    return os.path.getsize(dest)


def parse_args(argv: list[str]) -> tuple[str, str]:
    """拆出 (分享文案/链接, 输出根目录)。

    分享文案里可能有空格、& 等字符，所以除 -o/--out 及其取值之外的参数一律原样拼回文案；
    不传 -o 时输出根目录仍是脚本所在目录（保持旧行为，AstrBot/手动调用都不受影响）。
    """
    text_parts: list[str] = []
    out_root = BASE
    index = 0
    while index < len(argv):
        item = argv[index]
        if item in ("-o", "--out"):
            if index + 1 >= len(argv):
                print(f"{item} 后面要跟一个输出目录")
                sys.exit(1)
            out_root = os.path.abspath(argv[index + 1])
            index += 2
            continue
        if item.startswith("--out="):
            out_root = os.path.abspath(item.split("=", 1)[1])
            index += 1
            continue
        text_parts.append(item)
        index += 1
    return " ".join(text_parts).strip(), out_root


def main() -> None:
    raw, out_root = parse_args(sys.argv[1:])
    if not raw:
        print("用法: python douyin_downloader.py <视频URL|v.douyin.com短链|分享文案> [-o 输出根目录]")
        sys.exit(1)
    try:
        url = resolve_to_video_url(raw)
    except ValueError as e:
        print(e)
        sys.exit(1)
    print("解析到:", url, flush=True)

    ad = fetch_aweme_detail(url)
    nickname = ad["author"]["nickname"]
    aweme_id = str(ad["aweme_id"])
    desc = (ad.get("desc") or "").strip()
    create_time = ad.get("create_time")
    video = ad.get("video") or {}
    urls = (video.get("play_addr_h264") or video.get("play_addr") or {}).get("url_list") or []

    if not urls:
        print("没有可用的视频直链")
        sys.exit(1)

    title_field = re.sub(r"\s+", " ", desc).strip() if desc else "（无文案）"
    t = time.localtime(create_time) if create_time else time.localtime()
    pub_time = time.strftime("%y-%m-%d %H:%M", t) if create_time else "未知"
    ts = time.strftime("%Y-%m-%d-%H-%M", t)

    out_dir = os.path.join(out_root, sanitize(nickname), ts)
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, aweme_id + ".mp4")

    last_err = None
    for u in urls:
        try:
            size = download(u, out_path)
            print("下载完成:", out_path, f"({size} bytes)")
            break
        except Exception as e:  # noqa: BLE001
            last_err = e
            print("下载失败，尝试下一个:", e)
    else:
        print("全部直链失败:", last_err)
        sys.exit(1)

    context = (
        "# 抖音内容\n\n"
        f"- **作者**: {nickname}\n"
        f"- **作者UID**: {ad['author'].get('uid', '')}\n"
        f"- **作者sec_uid**: {ad['author'].get('sec_uid', '')}\n"
        f"- **原文链接**: https://www.douyin.com/video/{aweme_id}\n"
        f"- **发布时间**: {pub_time}\n"
        f"- **视频标题**: {title_field}\n"
        f"- **视频文件**: {aweme_id}.mp4\n"
    )
    ctx_path = os.path.join(out_dir, "context.md")
    with open(ctx_path, "w", encoding="utf-8") as f:
        f.write(context)
    print("context.md 已生成:", ctx_path)


if __name__ == "__main__":
    main()
