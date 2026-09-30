#!/usr/bin/env python3
"""
微博内容下载器 (weibo_downloader.py)
纯代码实现，不依赖 AI agent。

功能：
  1. 打开微博帖子页面，提取正文文本、发布时间、作者用户名、图片信息
  2. 按 用户名/yy-mm-dd 格式创建文件夹
  3. 下载普通图片（原图）到 <用户名>/<日期>/photo/
  4. 下载 Live 图（.mov 视频 + .jpg 封面）到 <用户名>/<日期>/live/
  5. 保存文本内容到 <用户名>/<日期>/context.md

用法：
  python weibo_downloader.py -url https://weibo.com/1234567890/AbCdEfGh
  python weibo_downloader.py -url https://weibo.com/xxx/yyy --out D:/data --headed

依赖：
  pip install playwright requests opencv-python
  python -m playwright install chromium
"""

import argparse
import os
import re
import sys
import time

# Windows 控制台默认 GBK，强制用 UTF-8 输出避免中文/emoji 报错
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import cv2
import requests
from playwright.sync_api import sync_playwright

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36")

HEADERS = {
    "Referer": "https://weibo.com/",
    "User-Agent": UA,
    "Accept": "*/*",
}


# ---------------------------------------------------------------- 工具函数

def parse_publish_time(text: str) -> str:
    """从帖子文本或 created_at 中提取 yy-mm-dd，找不到就用当天日期。"""
    # 格式1: 26-8-27 15:51（页面 innerText）
    m = re.search(r"(\d{2})-(\d{1,2})-(\d{1,2})", text)
    if m:
        y, mo, d = m.groups()
        return f"{y}-{int(mo):02d}-{int(d):02d}"
    # 格式2: Sun Aug 09 19:56:59 +0800 2026（API created_at）
    m = re.search(r"(\w{3}) (\w{3}) (\d{2}) \d{2}:\d{2}:\d{2} \+0800 (\d{4})", text)
    if m:
        months = {
            "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
            "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
        }
        year = m.group(4)[2:]
        mon = months.get(m.group(2), 1)
        day = int(m.group(3))
        return f"{year}-{mon:02d}-{day:02d}"
    return time.strftime("%y-%m-%d")


def parse_publish_datetime(text: str) -> str:
    """
    从 created_at（如 'Sun Aug 09 19:56:59 +0800 2026'）提取 yy-mm-dd hh:mm。
    失败则退回 parse_publish_time 的日期格式。
    """
    m = re.search(r"(\w{3}) (\w{3}) (\d{2}) (\d{2}):(\d{2}):\d{2} \+0800 (\d{4})", text)
    if m:
        months = {
            "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
            "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
        }
        year = m.group(6)[2:]
        mon = months.get(m.group(2), 1)
        day = int(m.group(3))
        hh = int(m.group(4))
        mm = int(m.group(5))
        return f"{year}-{mon:02d}-{day:02d} {hh:02d}:{mm:02d}"
    return parse_publish_time(text)


def sanitize_filename(name: str) -> str:
    """清理 Windows 文件名中的非法字符，并去掉首尾空白。"""
    name = re.sub(r'[\\/:*?"<>|]', "", name).strip()
    return name or "unknown"


def extract_username(text: str) -> str:
    """从帖子文本中提取作者用户名（时间行上面一行，跳过"公开"等状态词）。"""
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    for i, line in enumerate(lines):
        if re.match(r"^\d{2}-\d{1,2}-\d{1,2}\s+\d{2}:\d{2}", line):
            j = i - 1
            while j >= 0 and lines[j] in ("公开", "仅粉丝可见", "仅自己可见"):
                j -= 1
            if j >= 0:
                return sanitize_filename(lines[j])
    # 兜底：时间行在第二行，第一行可能就是用户名
    if len(lines) >= 2:
        return sanitize_filename(lines[1])
    return "unknown"


def extract_ip_region(text: str) -> str:
    """从帖子文本中提取 IP 属地（如 '发布于 安徽' -> '安徽'）。"""
    m = re.search(r"发布于\s*([^\s\u3000]+)", text)
    if m:
        return m.group(1)
    return ""


def download_file(url: str, path: str, referer: str = "https://weibo.com/") -> bool:
    """带 Referer 下载文件（绕过微博反盗链），返回是否成功。"""
    headers = {"Referer": referer, "User-Agent": UA}
    try:
        with requests.get(url, headers=headers, timeout=60, stream=True) as r:
            if r.status_code != 200:
                print(f"    [x] HTTP {r.status_code}: {url[:80]}")
                return False
            with open(path, "wb") as f:
                for chunk in r.iter_content(65536):
                    f.write(chunk)
        return True
    except Exception as e:
        print(f"    [x] 下载失败 {url[:60]}: {e}")
        return False


def orj360_to_large(url: str) -> str:
    """
    微博压缩规格 URL 转原图 URL。
    压缩规格：orj360 / orj480 / orj960 / orj1080 / wap180 / wap360 / cmw960 / mw690 等，
    原图规格是 large/ 前缀（等价于 pic_infos 里的 largest，即"下载原图"）。
    """
    if not url:
        return url
    # 已是原图/大规格则不动（large / mw2000 / original / largest）
    if any(x in url for x in ("/large/", "/mw2000/", "/original/", "/largest/")):
        return url
    m = re.search(r"/(?:orj\d+|wap\d+|cmw\d+|mw\d+)/", url)
    if m:
        return url[: m.start() + 1] + "large" + url[m.end() - 1 :]
    return url


def extract_cover_from_mov(mov_path: str, jpg_path: str) -> bool:
    """用 OpenCV 从 .mov 提取第一帧作为 .jpg 封面。"""
    try:
        cap = cv2.VideoCapture(mov_path)
        if not cap.isOpened():
            return False
        ret, frame = cap.read()
        cap.release()
        if not ret:
            return False
        cv2.imwrite(jpg_path, frame, [cv2.IMWRITE_JPEG_QUALITY, 92])
        return True
    except Exception as e:
        print(f"    [x] 提取封面失败 {mov_path}: {e}")
        return False


# ---------------------------------------------------------------- 页面提取

def scroll_page_gradually(page):
    """缓慢滚动页面，触发图片/视频懒加载。"""
    try:
        for _ in range(8):
            page.mouse.wheel(0, 600)
            time.sleep(0.4)
        page.keyboard.press("Home")
        time.sleep(0.8)
    except Exception:
        pass


def fetch_status_api(page, url: str) -> dict:
    """
    通过微博官方接口 https://weibo.com/ajax/statuses/show 获取帖子完整数据。
    从 URL 提取 mblogid（最后一段），用页面会话的 cookie 请求。
    返回 API 返回的 data 字典；失败返回 None。
    """
    m = re.search(r"/weibo\.com/\d+/([A-Za-z0-9]+)", url)
    if not m:
        return None
    mblogid = m.group(1)
    api_url = f"https://weibo.com/ajax/statuses/show?id={mblogid}&locale=zh-CN&isGetLongText=true"
    try:
        result = page.evaluate(
            """async (apiUrl) => {
                const r = await fetch(apiUrl, { credentials: 'include' });
                if (!r.ok) return null;
                const j = await r.json();
                return j && j.ok ? j : null;
            }""",
            api_url,
        )
        return result
    except Exception:
        return None


def decode_live_video_url(video_field) -> str:
    """
    pic_infos[].video 两种形态：
      - 直接是完整 URL 字符串（最常见）
      - {序号: 字符} 分片字典，按序号拼接即完整 URL
    """
    if not video_field:
        return ""
    if isinstance(video_field, str):
        return video_field if "livephoto" in video_field else ""
    if isinstance(video_field, dict):
        try:
            keys = [k for k in video_field.keys() if str(k).isdigit()]
            keys.sort(key=lambda k: int(k))
            return "".join(str(video_field[k]) for k in keys)
        except Exception:
            return ""
    return ""


def extract_post(page, url: str) -> dict:
    """从已打开的页面提取帖子数据。优先走官方 API（一次拿到全部图片和 Live 视频）。"""
    # ---- 1. 官方 API 提取（最可靠） ----
    # 先等 visitor 重定向完成、页面稳定，确保会话 cookie 就绪
    try:
        page.wait_for_selector("article", timeout=30000)
    except Exception:
        pass
    try:
        page.wait_for_load_state("networkidle", timeout=30000)
    except Exception:
        pass
    time.sleep(1.5)

    api_data = fetch_status_api(page, url)
    if api_data:
        pic_infos = api_data.get("pic_infos") or {}
        pic_ids = api_data.get("pic_ids") or []

        # 普通图片 & Live 视频：按 pic_ids 顺序
        normal_imgs = []
        live_videos = []
        for pid in pic_ids:
            info = pic_infos.get(pid) or {}
            # 图片原图：largest（large/ 前缀）是"下载原图"规格，质量最高；
            # 其次 mw2000 / original / large（后两者是压缩版，仅兜底）
            orig = (
                (info.get("largest") or {}).get("url")
                or (info.get("mw2000") or {}).get("url")
                or (info.get("original") or {}).get("url")
                or (info.get("large") or {}).get("url")
                or ""
            )
            # Live 视频（type == 'livephoto' 时存在 video 分片）
            vurl = ""
            if (info.get("type") or "") == "livephoto":
                vurl = decode_live_video_url(info.get("video"))
            if vurl:
                live_videos.append(vurl)
            elif orig:
                normal_imgs.append(orig)

        # 文本/用户名/时间从 API 提取（比 innerText 更干净）
        full_text = api_data.get("text_raw") or api_data.get("text") or ""
        # 清理 HTML 标签
        full_text = re.sub(r"<[^>]+>", "", full_text)
        page_title = page.title()
        username = sanitize_filename((api_data.get("user") or {}).get("screen_name") or "")
        created = api_data.get("created_at") or ""
        # IP 属地：region_name 形如 '发布于 安徽'，去掉前缀
        ip_region = re.sub(r"^发布于\s*", "", api_data.get("region_name") or "").strip()

        print(f"[*] [API] 图片 {len(pic_ids)} 张, Live 视频 {len(live_videos)} 个, 用户 {username}")
        # created_at 优先（格式稳定），fallback 到文本
        # publish_time 用于文件夹名（仅日期）；publish_datetime 用于 context.md（含 hh:mm）
        if created:
            publish_datetime = parse_publish_datetime(created)
            publish_time = publish_datetime.split(" ")[0]
        else:
            publish_time = parse_publish_time(full_text)
            publish_datetime = publish_time
        return {
            "title": page_title,
            "text": full_text,
            "username": username,
            "created_at": created,
            "ip_region": ip_region,
            "publish_time": publish_time,
            "publish_datetime": publish_datetime,
            "normal_imgs": normal_imgs,
            "live_videos": live_videos,
            "api": True,
        }

    # ---- 2. 兜底：DOM 提取（API 失败时） ----
    live_video_urls = []
    seen = set()

    def on_response(resp):
        u = resp.url
        if "livephoto.us.sinaimg.cn" in u and ".mov" in u and u not in seen:
            seen.add(u)
            live_video_urls.append(u)

    page.on("response", on_response)

    try:
        page.wait_for_selector("article", timeout=30000)
    except Exception:
        pass
    try:
        page.wait_for_load_state("networkidle", timeout=30000)
    except Exception:
        pass
    time.sleep(2)
    scroll_page_gradually(page)

    art = page.query_selector("article")
    if not art:
        raise RuntimeError("未找到帖子内容，可能被微博反爬拦截，请重试或使用 --headed 观察")

    full_text = art.inner_text()
    page_title = page.title()

    normal_imgs = []
    for img in page.query_selector_all("article img.woo-picture-img"):
        src = img.get_attribute("src") or img.get_attribute("currentSrc") or ""
        if "sinaimg" in src:
            normal_imgs.append(src)

    live_posters = []
    for _ in range(6):
        live_posters = page.query_selector_all('article img[class*="customPoster"]')
        if len(live_posters) >= 1:
            time.sleep(1.5)
            again = page.query_selector_all('article img[class*="customPoster"]')
            if len(again) == len(live_posters):
                break
    print(f"[*] [DOM] 普通图片 {len(normal_imgs)} 张, Live 图 {len(live_posters)} 张")

    # 逐个点击 Live 图触发视频加载
    live_poster_srcs = []
    for el in live_posters:
        s = (el.get_attribute("src") or "").split("?")[0]
        if s and s not in live_poster_srcs:
            live_poster_srcs.append(s)

    for src in live_poster_srcs:
        target = None
        for img in page.query_selector_all("article img[src]"):
            if (img.get_attribute("src") or "").split("?")[0] == src:
                target = img
                break
        if target is None:
            continue
        try:
            target.scroll_into_view_if_needed(timeout=5000)
            target.click(timeout=5000)
        except Exception:
            continue
        time.sleep(3.0)
        try:
            back = page.query_selector("._back_137iq_72")
            if back:
                back.click(timeout=3000)
            else:
                page.keyboard.press("Escape")
        except Exception:
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
        try:
            page.wait_for_selector("[class*=previewList]", state="detached", timeout=5000)
        except Exception:
            pass
        time.sleep(1.0)

    # 兜底收集：performance 缓存 + DOM video
    try:
        perf = page.evaluate(
            "() => performance.getEntriesByType('resource')"
            ".map(r => r.name).filter(n => n.includes('livephoto.us.sinaimg.cn') && n.includes('.mov'))"
        )
        for u in perf:
            if u and u not in seen:
                seen.add(u)
                live_video_urls.append(u)
    except Exception:
        pass

    if len(live_video_urls) < len(live_posters):
        try:
            vids = page.eval_on_selector_all(
                "video",
                "els => els.map(v => v.currentSrc || v.src).filter(s => s && s.includes('livephoto'))",
            )
            for v in vids:
                if v and v not in seen:
                    seen.add(v)
                    live_video_urls.append(v)
        except Exception:
            pass

    print(f"[*] [DOM] 捕获到 {len(live_video_urls)} 个 Live 视频")

    seen_n = set()
    normal_imgs = [u for u in normal_imgs if not (u in seen_n or seen_n.add(u))]
    live_img_urls = {
        (el.get_attribute("src") or "").split("?")[0]
        for el in live_posters
    }
    normal_imgs = [u for u in normal_imgs if u.split("?")[0] not in live_img_urls]

    return {
        "title": page_title,
        "text": full_text,
        "username": extract_username(full_text),
        "created_at": "",
        "ip_region": extract_ip_region(full_text),
        "publish_time": parse_publish_time(full_text),
        "publish_datetime": parse_publish_time(full_text),
        "normal_imgs": normal_imgs,
        "live_videos": live_video_urls,
        "api": False,
    }


# ---------------------------------------------------------------- 保存

def save_content(data: dict, out_root: str) -> str:
    # 保存结构: <out_root>/<用户名>/<yy-mm-dd>/
    folder = os.path.join(out_root, data["username"], data["publish_time"])
    # 只有存在对应内容时才创建 photo/ 或 live/ 文件夹
    has_photos = bool(data["normal_imgs"])
    has_lives = bool(data["live_videos"])
    photo_dir = os.path.join(folder, "photo") if has_photos else None
    live_dir = os.path.join(folder, "live") if has_lives else None
    os.makedirs(folder, exist_ok=True)
    if photo_dir:
        os.makedirs(photo_dir, exist_ok=True)
    if live_dir:
        os.makedirs(live_dir, exist_ok=True)
    print(f"[*] 保存目录: {folder}")

    # 1. 下载普通图片
    if photo_dir:
        print(f"[*] 下载 {len(data['normal_imgs'])} 张普通图片 -> {photo_dir}")
        for idx, url in enumerate(data["normal_imgs"], 1):
            large = orj360_to_large(url)
            path = os.path.join(photo_dir, f"img{idx:02d}.jpg")
            if download_file(large, path):
                print(f"    [+] img{idx:02d}.jpg  <- {large.split('/')[-1][:40]}")
    else:
        print("[*] 无普通图片，跳过 photo/")

    # 2. 下载 Live 视频 + 提取封面
    if live_dir:
        print(f"[*] 下载 {len(data['live_videos'])} 个 Live 视频 -> {live_dir}")
        for idx, url in enumerate(data["live_videos"], 1):
            name = f"live{idx:02d}"
            mov_path = os.path.join(live_dir, f"{name}.mov")
            jpg_path = os.path.join(live_dir, f"{name}.jpg")
            if download_file(url, mov_path):
                print(f"    [+] {name}.mov")
                if extract_cover_from_mov(mov_path, jpg_path):
                    print(f"    [+] {name}.jpg (封面)")
    else:
        print("[*] 无 Live 图，跳过 live/")

    # 3. 写 context.md
    text = data["text"]
    md = []
    md.append("# 微博内容\n")
    md.append(f"- **作者**: {data['username']}")
    md.append(f"- **原文链接**: {data.get('url', '')}")
    md.append(f"- **发布时间**: {data.get('publish_datetime', data['publish_time'])}")
    if data.get("ip_region"):
        md.append(f"- **IP属地**: {data['ip_region']}")
    if has_photos:
        md.append(f"- **图片数量**: {len(data['normal_imgs'])} 张 (photo/)")
    if has_lives:
        md.append(f"- **Live 图数量**: {len(data['live_videos'])} 个 (live/)\n")
    md.append("## 页面文本\n")
    md.append("```")
    md.append(text)
    md.append("```\n")
    md_path = os.path.join(folder, "context.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(md))
    print(f"[*] 已保存: {md_path}")
    return folder


# ---------------------------------------------------------------- 主流程

def main():
    ap = argparse.ArgumentParser(description="微博内容下载器：文本 + 图片 + Live 图(mov+jpg)")
    ap.add_argument("-url", required=True, help="微博帖子链接，如 https://weibo.com/1234567890/AbCdEfGh")
    ap.add_argument("--out", default=".", help="输出根目录（默认当前目录），脚本会创建 <用户名>/<yy-mm-dd>/ 子目录")
    ap.add_argument("--headed", action="store_true", help="显示浏览器窗口（默认无头）")
    ap.add_argument("--timeout", type=int, default=60000, help="页面加载超时毫秒（默认 60000）")
    args = ap.parse_args()

    url = args.url.strip()
    print(f"[*] 目标: {url}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed)
        ctx = browser.new_context(
            user_agent=UA,
            viewport={"width": 1280, "height": 900},
            locale="zh-CN",
        )
        page = ctx.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=args.timeout)
            data = extract_post(page, url)
            data["url"] = url
        except Exception as e:
            print(f"[!] 提取失败: {e}")
            browser.close()
            sys.exit(1)
        browser.close()

    folder = save_content(data, os.path.abspath(args.out))
    print(f"\n✅ 完成！文件保存在: {folder}")
    print("   - photo/   普通图片 (原图)")
    print("   - live/    Live 图 (.mov 视频 + .jpg 封面)")
    print("   - context.md  文本内容")


if __name__ == "__main__":
    main()
