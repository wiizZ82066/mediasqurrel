"""统一浏览器引擎：替代 scrapling 的 DynamicFetcher（本项目用到的部分）。

- launch_chrome(): 系统 Chrome (channel='chrome')，无 Chrome 时退回自带 chromium
  —— 等价于 scrapling 的 real_chrome=True
- XHRHunter: 复刻 capture_xhr（正则匹配响应 URL 并缓存响应体）
- USE_PATCHRIGHT 开关: 抖音风控升级导致失效时改为 True
  （patchright 是 playwright 的反检测 fork，API 完全兼容，pip install patchright 即可）
"""
import re

# 风控升级应急开关：True 时使用 patchright（playwright 反检测 fork）
USE_PATCHRIGHT = False

_LAZY = {}


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
