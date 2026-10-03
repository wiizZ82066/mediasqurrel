"""统一浏览器引擎：替代 scrapling 的 DynamicFetcher（本项目用到的部分）。

- launch_chrome(): 系统 Chrome (channel='chrome')，无 Chrome 时退回自带 chromium
  —— 等价于 scrapling 的 real_chrome=True
- stealth_context(): 创建已处理 UA 的 context
  · 首选: 读取当前浏览器真实 navigator.userAgent 并清除 Headless 标记
    （与本机 Chrome 版本 100% 一致，避免 UA 与浏览器特征不匹配）
  · 兜底: browserforge 生成真实世界分布的 Chrome UA（scrapling 同款引擎）
- XHRHunter: 复刻 capture_xhr（正则匹配响应 URL 并缓存响应体）
- USE_PATCHRIGHT 开关: 抖音风控升级导致失效时改为 True
  （patchright 是 playwright 的反检测 fork，API 完全兼容，pip install patchright 即可）
"""
import re

# 风控升级应急开关：True 时使用 patchright（playwright 反检测 fork）
USE_PATCHRIGHT = False

# browserforge 不可用时的最终兜底（尽量保持与 Chrome 版本无关的写法）
_FALLBACK_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/{ver} Safari/537.36"
).format(ver="130.0.0.0")


def sync_playwright():
    """惰性选择 playwright / patchright（import 兼容，API 相同）。"""
    if USE_PATCHRIGHT:
        try:
            from patchright.sync_api import sync_playwright as _sp
            return _sp()
        except ImportError:
            pass
    from playwright.sync_api import sync_playwright as _sp
    return _sp()


def launch_chrome(p, headless: bool = True):
    """启动系统 Chrome；未安装 Chrome 时退回 playwright 自带 chromium。"""
    try:
        return p.chromium.launch(channel="chrome", headless=headless)
    except Exception:
        return p.chromium.launch(headless=headless)


def generate_headers() -> dict:
    """browserforge 生成真实 Chrome 桌面 header 集（UA/accept-language 等一致）。

    scrapling 的 UA 生成即此引擎（browserforge），独立于 scrapling 可单独使用。
    未安装 browserforge 时返回空 dict。
    """
    try:
        from browserforge.headers import HeaderGenerator
        hg = HeaderGenerator(browser=("chrome",), device="desktop")
        headers = dict(hg.generate())
        # 统一小写键，UA 键兼容 user-agent/User-Agent
        if "User-Agent" in headers and "user-agent" not in headers:
            headers["user-agent"] = headers.pop("User-Agent")
        return headers
    except Exception:
        return {}


def get_ua() -> str:
    """获取一个随机真实 Chrome UA（browserforge），无依赖时用内置兜底。"""
    return generate_headers().get("user-agent") or _FALLBACK_UA


def _probe_real_ua(browser) -> str | None:
    """读取浏览器真实 UA 并清除 headless 标记（与实际浏览器特征完全一致）。"""
    try:
        probe = browser.new_context()
        page = probe.new_page()
        ua = page.evaluate("navigator.userAgent")
        probe.close()
        if ua:
            return ua.replace("HeadlessChrome", "Chrome")
    except Exception:
        pass
    return None


def stealth_context(browser, locale: str = "zh-CN"):
    """创建已处理 UA 的浏览器 context（stealth 第一步：UA 与浏览器一致）。"""
    ua = _probe_real_ua(browser) or get_ua()
    return browser.new_context(user_agent=ua, locale=locale)


class XHRHunter:
    """capture_xhr 等价物：附加到 page，按正则捕获 XHR/fetch 响应。

    用法:
        hunter = XHRHunter(r"aweme/v1/web/aweme/detail")
        hunter.attach(page)
        page.goto(...) / 等待...
        for xhr in hunter.results:   # {"url", "status", "body"(str)}
    """

    def __init__(self, pattern: str):
        self._re = re.compile(pattern)
        self._hits: list[dict] = []

    def _on_response(self, resp):
        try:
            url = resp.url
        except Exception:
            return
        if not self._re.search(url):
            return
        try:
            body = resp.body()
        except Exception:
            body = b""  # 响应被导航冲掉等场景
        if isinstance(body, bytes):
            body = body.decode("utf-8", errors="replace")
        self._hits.append({"url": url, "status": resp.status, "body": body})

    def attach(self, page) -> "XHRHunter":
        page.on("response", self._on_response)
        return self

    @property
    def results(self) -> list[dict]:
        return self._hits

    def json_results(self) -> list[dict]:
        """解析为 JSON 的结果（解析失败的跳过）。"""
        import json
        out = []
        for h in self._hits:
            try:
                out.append(json.loads(h["body"]))
            except Exception:
                continue
        return out
