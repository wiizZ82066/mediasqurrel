"""微博博主扫描器：检测博主主页新微博。

技术路线（已实测可行）:
  PC 端 ajax/statuses/mymblog 需要登录态（访客 403 / ok:0），
  移动端 m.weibo.cn 主页会自带请求 container/getIndex（containerid=107603{uid}），
  用 playwright 以移动端 UA 打开主页并拦截该响应即可匿名拿到微博列表。

去重: watcher 层用 seen_items 表按 bid 记录；首次扫描只"对齐基线"不下载历史。
"""
import asyncio
import re

from playwright.sync_api import sync_playwright

from .. import watcher

MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text or "").replace("\n", " ").strip()


def _scan_sync(sub: dict) -> list[dict]:
    uid = str(sub["blogger_id"]).strip()
    captured: list[dict] = []

    def on_response(resp):
        try:
            if f"containerid=107603{uid}" in resp.url:
                captured.append(resp.json())
        except Exception:
            pass

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent=MOBILE_UA,
            locale="zh-CN",
            viewport={"width": 390, "height": 844},
            is_mobile=True,
            has_touch=True,
        )
        page = ctx.new_page()
        page.on("response", on_response)
        try:
            page.goto(
                f"https://m.weibo.cn/u/{uid}",
                wait_until="domcontentloaded",
                timeout=60000,
            )
            page.wait_for_timeout(6000)
        finally:
            browser.close()

    items = []
    for j in captured:
        if j.get("ok") != 1:
            continue
        for card in (j.get("data") or {}).get("cards") or []:
            mb = card.get("mblog") or {}
            bid = mb.get("bid")
            if not bid:
                continue
            # 过滤转发的微博（转发内容归原博主，避免重复下载）
            if mb.get("retweeted_status"):
                continue
            items.append({
                "item_id": bid,
                "script_id": "weibo",
                "params": {"url": f"https://weibo.com/{uid}/{bid}"},
                "title": _strip_html(mb.get("text"))[:60] or "（无正文）",
                "created_at": mb.get("created_at") or "",
            })
    return items


def scan(sub: dict):
    """watcher 注册入口：返回 coroutine（在线程中跑 playwright 同步 API）。"""
    return asyncio.to_thread(_scan_sync, sub)


watcher.register_scanner("weibo", scan)
