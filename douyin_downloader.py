"""抖音视频/图集下载器（默认 Playwright；媒体由本机保存）。

用法:
    python douyin_downloader.py <作品URL|v.douyin.com短链|分享文案> [-o 输出根目录]

流程:
    1. 打开作品页，读取匹配作品的详情接口或公开的页面数据
    2. 视频保存 mp4 和官方封面，图集按顺序保存实际格式的图片
    3. 在独立作品目录生成 context.md 与完整性清单 entry.json

-o/--out 不传时使用配置中的独立媒体库根目录。
"""
import json
from http.client import HTTPException as HTTPTransferError
import os
import re
import sys
import time
import tempfile
import urllib.request
import warnings
from pathlib import Path
from urllib.parse import urljoin, urlsplit

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError, ValueError):
    pass

import requests
from app.progress import DownloadProgress
from app import config
from app.archive import atomic_output, atomic_write_text, begin_entry
from app.douyin_content import album_images, content_id, is_album, is_douyin_url, media_urls, source_url
from app.platform_errors import PlatformAccessError
from app.douyin_page import detail_from_scripts

# 统一浏览器引擎（app/browser.py；脚本独立运行时按相对路径加载）
try:
    from app.browser import XHRHunter, get_ua, launch_chrome, stealth_context, sync_playwright
except ImportError:
    import importlib.util
    _spec = importlib.util.spec_from_file_location(
        "_ms_browser", os.path.join(os.path.dirname(os.path.abspath(__file__)), "app", "browser.py")
    )
    _mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    XHRHunter, launch_chrome, stealth_context, sync_playwright = (
        _mod.XHRHunter, _mod.launch_chrome, _mod.stealth_context, _mod.sync_playwright
    )
    get_ua = _mod.get_ua

# 保留 BASE 供旧调用者读取；实际 CLI 默认值每次从配置获取。
BASE = config.LIBRARY_ROOT

# UA 按需惰性生成（browserforge 随机真实 Chrome UA），不再写死
_UA_CACHE = None


def _ua() -> str:
    global _UA_CACHE
    if not _UA_CACHE:
        _UA_CACHE = get_ua()
    return _UA_CACHE


def sanitize(s: str) -> str:
    s = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", str(s)).strip().strip(".")
    return s or "untitled"


_URL_RE = re.compile(r"https?://[A-Za-z0-9./?=&_%:@+~#-]+")


def extract_douyin_url(text: str) -> str | None:
    """从分享文案里提取第一个抖音链接。"""
    for u in _URL_RE.findall(text):
        candidate = u.rstrip(".,;:!?。，；：！？")
        if is_douyin_url(candidate):
            return candidate
    return None


def extract_video_id(url: str) -> str | None:
    """Compatibility name: both video and note URLs identify a post."""
    return content_id(url)


def resolve_to_video_url(raw: str) -> str:
    """Compatibility name: retain note URLs, including short-link redirects."""
    url = extract_douyin_url(raw)
    if not url:
        raise ValueError("未找到抖音链接")
    for _ in range(6):
        vid = extract_video_id(url)
        if vid:
            kind = 'note' if any(f'/{kind}/' in urlsplit(url).path for kind in ('note', 'slides')) else 'video'
            return f"https://www.douyin.com/{kind}/{vid}"
        # Check each redirect before following it; never fetch an unrelated host.
        try:
            with requests.get(url, headers={"User-Agent": _ua()}, allow_redirects=False,
                              stream=True, timeout=30) as response:
                if response.status_code not in (301, 302, 303, 307, 308):
                    break
                target = urljoin(url, response.headers.get('Location', ''))
        except requests.RequestException:
            raise ValueError('抖音分享链接解析失败，请检查网络后重试') from None
        if not is_douyin_url(target) or target == url:
            break
        url = target
    raise ValueError('无法从分享链接解析出作品 ID，请复制完整的视频或图文作品链接')


def _attach_dy_cookies(ctx):
    """注入本地保存的抖音登录态（若存在）。"""
    try:
        try:
            from app.douyin_auth import attach_cookies
        except ImportError:
            import importlib.util as ilu
            # 源码运行在脚本目录；frozen 后端在 PyInstaller 资源目录
            for base in (config.RESOURCE_DIR, getattr(sys, "_MEIPASS", "")):
                if not base:
                    continue
                p = os.path.join(base, "app", "douyin_auth.py")
                if os.path.isfile(p):
                    s = ilu.spec_from_file_location("_dy_auth", p)
                    m = ilu.module_from_spec(s)
                    s.loader.exec_module(m)
                    attach_cookies = m.attach_cookies
                    break
            else:
                return
        attach_cookies(ctx)
    except Exception:
        pass  # 无登录态时静默跳过


def _fetch_aweme_detail_once(url: str) -> dict | None:
    """Return matching detail as soon as it arrives; do not sleep six extra seconds."""
    expected = content_id(url)
    with sync_playwright() as p:
        browser = launch_chrome(p, headless=True)
        try:
            ctx = stealth_context(browser)
            _attach_dy_cookies(ctx)
            page = ctx.new_page()
            hunter = XHRHunter(r"aweme/v1/web/aweme/detail").attach(page)
            response = page.goto(url, wait_until="domcontentloaded", timeout=60000,
                                 referer="https://www.google.com/")
            if response is not None:
                _check_access_status(response.status)
            deadline = time.monotonic() + 6
            checked = 0
            next_page_check = 0
            while True:
                hits = hunter.results
                for hit in hits[checked:]:
                    _check_access_status(hit.get('status'))
                    try:
                        payload = json.loads(hit['body'])
                    except (ValueError, TypeError):
                        raise PlatformAccessError('抖音详情未返回有效数据，请检查登录或平台验证', 'invalid_response', platform='douyin') from None
                    if not isinstance(payload, dict) or payload.get('status_code', 0) not in (0, '0'):
                        raise PlatformAccessError('抖音未允许读取作品详情，请检查登录或平台验证', platform='douyin')
                    detail = payload.get('aweme_detail')
                    if isinstance(detail, dict) and str(detail.get('aweme_id')) == expected:
                        return detail
                checked = len(hits)
                if time.monotonic() >= next_page_check:
                    scripts = page.locator('script').evaluate_all("""nodes => {
                        const result = []; let total = 0;
                        for (const node of nodes) {
                            const text = node.textContent;
                            if (node.id !== 'RENDER_DATA' && !text.startsWith('self.__pace_f.push(')) continue;
                            total += text.length;
                            if (total > 8 * 1024 * 1024) return null;
                            result.push(text);
                        }
                        return result;
                    }""")
                    if scripts is None:
                        raise PlatformAccessError('抖音页面数据过大，已停止解析', 'invalid_response', platform='douyin')
                    detail = detail_from_scripts(scripts, expected)
                    if detail:
                        return detail
                    next_page_check = time.monotonic() + .5
                if time.monotonic() >= deadline:
                    break
                page.wait_for_timeout(200)
        finally:
            browser.close()
    raise PlatformAccessError('未收到此作品的完整详情，请检查作品可见性、登录或平台验证后重试', 'invalid_response', platform='douyin')


def _check_access_status(status):
    if status in (401, 403, 429):
        reason = {401: 'login_required', 403: 'risk_control', 429: 'rate_limited'}[status]
        raise PlatformAccessError('抖音请求被拒绝，请检查登录、平台验证或限流状态后手动重试', reason, platform='douyin')
    if status is not None and status >= 500:
        raise PlatformAccessError('抖音暂时无法返回作品详情，请稍后重试', 'platform_error', platform='douyin')


def fetch_aweme_detail(url: str) -> dict:
    """Browser remains default; an optional adapter never bypasses access errors."""
    from playwright.sync_api import Error as BrowserError, TimeoutError as BrowserTimeout
    channel = os.environ.get('MS_DOUYIN_DETAIL_CHANNEL', 'browser')
    if channel not in ('browser', 'catbus'):
        raise ValueError('抖音解析方式无效')
    started = time.monotonic()
    if channel == 'catbus':
        from app.douyin_detail_adapter import fetch_detail
        try:
            detail = fetch_detail(content_id(url), timeout=max(10, min(120, int(os.environ.get('MS_DOWNLOAD_TIMEOUT', '120')))))
            print(f'详情解析完成：实验性接口，耗时 {time.monotonic() - started:.2f} 秒', flush=True)
            return detail
        except PlatformAccessError as error:
            if not error.fallback_allowed:
                raise
            print('实验性接口尚不可用，本次使用浏览器解析；可在设置中恢复默认方式', flush=True)
    last_err = None
    for attempt in range(1 + max(0, min(5, int(os.environ.get('MS_DOWNLOAD_RETRIES', '2'))))):
        if attempt:
            time.sleep(3)  # Only transport timeouts/errors are retried.
            print(f"[*] 第 {attempt + 1} 次尝试（{url.split('/')[-1][:24]}）…")
        try:
            detail = _fetch_aweme_detail_once(url)
        except PlatformAccessError as error:
            if not error.retryable:
                raise
            last_err = error
            continue
        except BrowserTimeout:
            last_err = PlatformAccessError('浏览器读取抖音作品超时，请检查网络后重试', 'timeout', platform='douyin')
            continue
        except BrowserError:
            last_err = PlatformAccessError('浏览器无法读取抖音作品，请检查浏览器环境和网络', 'network_error', platform='douyin')
            continue
        if detail:
            print(f'详情解析完成：浏览器，耗时 {time.monotonic() - started:.2f} 秒', flush=True)
            return detail
        raise PlatformAccessError('未捕获到抖音详情，已停止重试，请检查平台验证', 'invalid_response', platform='douyin')
    raise last_err from None


def download(url: str, dest: str, progress=None) -> int:
    attempts = 1 + max(0, min(5, int(os.environ.get('MS_DOWNLOAD_RETRIES', '2'))))
    for attempt in range(attempts):
        try:
            return _download_once(url, dest, progress)
        except Exception:
            if attempt == attempts - 1:
                raise
            time.sleep(min(attempt + 1, 3))


def _download_once(url: str, dest: str, progress=None) -> int:
    with atomic_output(dest) as temporary:
        _transfer(url, temporary, progress)
    if progress:
        progress.finish_file()
    return os.path.getsize(dest)


def _transfer(url, temporary, progress=None):
    req = urllib.request.Request(
        url,
        headers={"User-Agent": _ua(), "Referer": "https://www.douyin.com/"},
    )
    with urllib.request.urlopen(req, timeout=max(10, min(600, int(os.environ.get('MS_DOWNLOAD_TIMEOUT', '120'))))) as resp, open(temporary, "wb") as f:
        if progress:
            progress.start_file(resp.headers.get("Content-Length"))
        written = 0
        while True:
            chunk = resp.read(1 << 16)
            if not chunk:
                break
            f.write(chunk)
            written += len(chunk)
            if progress:
                progress.advance(len(chunk))
        length = resp.headers.get("Content-Length")
        if not written or (length and length.isdigit() and written != int(length)):
            raise ValueError("下载内容为空或长度不完整")


def _image_extension(path):
    """Check actual image bytes before publishing a correctly named media file."""
    from PIL import Image
    extensions = {'JPEG': '.jpg', 'PNG': '.png', 'WEBP': '.webp', 'GIF': '.gif'}
    with warnings.catch_warnings():
        warnings.simplefilter('error', Image.DecompressionBombWarning)
        with Image.open(path) as picture:
            extension = extensions.get(picture.format)
            if extension is None:
                raise ValueError('图片格式暂不支持，尝试其他图片地址')
            picture.verify()
        with Image.open(path) as picture:
            picture.load()  # JPEG verify alone does not detect truncated pixel data.
    return extension


def download_image(urls, base, progress=None):
    """Try each candidate; failures never count as a completed image."""
    base = Path(base)
    base.parent.mkdir(parents=True, exist_ok=True)
    attempts = 1 + max(0, min(5, int(os.environ.get('MS_DOWNLOAD_RETRIES', '2'))))
    for url in urls:
        for attempt in range(attempts):
            fd, temporary = tempfile.mkstemp(prefix='.' + base.name + '.', suffix='.part', dir=base.parent)
            os.close(fd)
            try:
                _transfer(url, temporary, progress)
                destination = base.with_suffix(_image_extension(temporary))
                with open(temporary, 'r+b') as stream:
                    os.fsync(stream.fileno())
                os.replace(temporary, destination)
                if progress:
                    progress.finish_file()
                return destination
            except (OSError, ValueError, Warning, SyntaxError, HTTPTransferError):
                if attempt + 1 < attempts:
                    time.sleep(min(attempt + 1, 3))
            finally:
                Path(temporary).unlink(missing_ok=True)
        print('图片地址不可用，正在尝试备用地址…', flush=True)
    raise RuntimeError('图片所有候选地址均下载或校验失败；存档保持未完成，可重试') from None


def parse_args(argv: list[str]) -> tuple[str, str]:
    """拆出 (分享文案/链接, 输出根目录)。

    分享文案里可能有空格、& 等字符，所以除 -o/--out 及其取值之外的参数一律原样拼回文案；
    不传 -o 时使用配置的媒体库根；显式输出目录保持兼容。
    """
    text_parts: list[str] = []
    out_root = config.LIBRARY_ROOT
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
        print("用法: python douyin_downloader.py <作品URL|v.douyin.com短链|分享文案> [-o 输出根目录]")
        sys.exit(1)
    try:
        url = resolve_to_video_url(raw)
    except ValueError as e:
        print(e)
        sys.exit(1)
    print("解析到:", url, flush=True)

    ad = fetch_aweme_detail(url)
    save_content(ad, out_root)


def save_content(ad: dict, out_root: str) -> str:
    """Save a single resolved post; separate from network extraction for testing."""
    if is_album(ad):
        return _save_album(ad, out_root)
    nickname = ad["author"]["nickname"]
    aweme_id = str(ad["aweme_id"])
    desc = (ad.get("desc") or "").strip()
    create_time = ad.get("create_time")
    video = ad.get("video") or {}
    urls = list(dict.fromkeys(media_urls(video.get('play_addr_h264')) + media_urls(video.get('play_addr'))))

    if not urls:
        print("没有可用的视频直链")
        raise RuntimeError("没有可用的视频直链")

    title_field = re.sub(r"\s+", " ", desc).strip() if desc else "（无文案）"
    t = time.localtime(create_time) if create_time else time.localtime()
    pub_time = time.strftime("%y-%m-%d %H:%M", t) if create_time else "未知"
    ts = time.strftime("%Y-%m-%d-%H-%M", t)

    archive = begin_entry(out_root, sanitize(nickname), ts, "douyin", aweme_id,
                          f"https://www.douyin.com/video/{aweme_id}")
    out_dir = archive.folder
    print("保存目录:", out_dir)
    if archive.existing_complete:
        print("此内容已有完整存档，已校验文件清单")
        return out_dir
    out_path = os.path.join(out_dir, aweme_id + ".mp4")
    files = ["context.md", aweme_id + ".mp4"]

    last_err = None
    cover_urls = list(dict.fromkeys(media_urls(video.get('origin_cover')) + media_urls(video.get('cover'))))
    progress = DownloadProgress(1 + bool(cover_urls))
    for u in urls:
        try:
            size = download(u, out_path, progress=progress)
            print("下载完成:", out_path, f"({size} bytes)")
            break
        except Exception as e:  # noqa: BLE001
            last_err = e
            print("视频地址不可用，尝试下一个地址")
    else:
        raise RuntimeError("全部视频直链下载失败") from None

    # 下载官方封面（origin_cover 优先，比抽帧质量高；供媒体库卡片使用）
    if cover_urls:
        cover_path = os.path.join(out_dir, aweme_id + "_cover.jpg")
        for cover_url in cover_urls:
            try:
                download(cover_url, cover_path, progress=progress)
                print("官方封面已保存:", cover_path)
                files.append(aweme_id + "_cover.jpg")
                break
            except Exception as e:  # noqa: BLE001
                last_err = e
                print("封面地址不可用，尝试下一个地址")
        else:
            raise RuntimeError("官方封面下载失败，存档保持未完成，可重试") from None

    progress.stage("正在保存正文与整理文件…")
    context = (
        "# 抖音内容\n\n"
        f"- **作者**: {nickname}\n"
        f"- **作者UID**: {ad['author'].get('uid', '')}\n"
        f"- **作者sec_uid**: {ad['author'].get('sec_uid', '')}\n"
        f"- **原文链接**: https://www.douyin.com/video/{aweme_id}\n"
        f"- **发布时间**: {pub_time}\n"
        f"- **视频标题**: {title_field}\n"
        f"- **视频文件**: {aweme_id}.mp4\n"
        + (f"- **封面文件**: {aweme_id}_cover.jpg\n" if cover_urls else "")
    )
    ctx_path = os.path.join(out_dir, "context.md")
    atomic_write_text(ctx_path, context)
    print("context.md 已生成:", ctx_path)
    archive.complete(files)
    return out_dir


def _save_album(ad, out_root):
    author = ad.get('author') or {}
    nickname = author.get('nickname')
    if not isinstance(nickname, str) or not nickname.strip():
        raise ValueError('图集作者资料缺失，无法建立存档')
    canonical = source_url(ad)
    identity = str(ad['aweme_id'])
    created = ad.get('create_time')
    date = time.localtime(created) if created else time.localtime()
    published = time.strftime('%y-%m-%d %H:%M', date) if created else '未知'
    archive = begin_entry(out_root, sanitize(nickname), time.strftime('%Y-%m-%d-%H-%M', date),
                          'douyin', identity, canonical)
    folder = Path(archive.folder)
    print('保存目录:', archive.folder, flush=True)
    if archive.existing_complete:
        print('此内容已有完整存档，已校验文件清单')
        return archive.folder
    text = str(ad.get('desc') or '').strip()
    title = re.sub(r'\s+', ' ', text).strip() or '（无文案）'
    # Keep the legacy title field for existing context.md readers and searches.
    context = ('# 抖音内容\n\n'
               f'- **作者**: {nickname}\n'
               f'- **作者UID**: {author.get("uid", "")}\n'
               f'- **作者sec_uid**: {author.get("sec_uid", "")}\n'
               f'- **原文链接**: {canonical}\n'
               f'- **发布时间**: {published}\n'
               '- **内容类型**: 图集\n'
               f'- **视频标题**: {title}\n')
    atomic_write_text(folder / 'context.md', context)
    candidates = album_images(ad)  # Missing one image is an error, never silently skip.
    progress = DownloadProgress(len(candidates))
    files = ['context.md']
    for index, urls in enumerate(candidates, 1):
        image = download_image(urls, folder / 'photo' / f'img{index:04d}', progress)
        relative = image.relative_to(folder).as_posix()
        files.append(relative)
        print(f'图集图片 {index}/{len(candidates)} 已保存: {relative}', flush=True)
        # On a retry a CDN may serve a different image format. Remove only this
        # slot's older generated variants after its replacement is verified.
        for ext in ('.jpg', '.png', '.webp', '.gif'):
            old = image.with_suffix(ext)
            if old != image:
                old.unlink(missing_ok=True)
        atomic_write_text(folder / 'context.md', context + '- **图片文件**: ' + ', '.join(files[1:]) + '\n')
    progress.stage('正在保存正文与整理文件…')
    archive.complete(files)
    return archive.folder


if __name__ == "__main__":
    main()
