"""抖音博主扫描器：检测博主主页新视频。

技术路线（已实测可行）:
  系统 Chrome (channel='chrome') 打开
  https://www.douyin.com/user/{sec_uid}，
  拦截主页自带的视频列表接口 aweme/v1/web/aweme/post 响应。

订阅标识: blogger_id 使用 sec_uid（长串 MS4wLjAB...）。
  获取方式: 视频页 aweme_detail 的 author.sec_uid，
  新下载的存档 context.md 里已自动记录。

去重: watcher 层用 seen_items 表按 aweme_id 记录；首次扫描只对齐基线。
注意: 主页列表可能含置顶旧视频，去重不依赖顺序，无影响。
"""
import re

from ..browser import XHRHunter, launch_chrome, stealth_context, sync_playwright
from .. import watcher


def _scan_sync(sub: dict) -> list[dict]:
    sec_uid = str(sub["blogger_id"]).strip()

    hunter = XHRHunter(r"aweme/v1/web/aweme/post")
    with sync_playwright() as p:
        browser = launch_chrome(p, headless=True)
        try:
            ctx = stealth_context(browser)
            page = ctx.new_page()
            hunter.attach(page)
            page.goto(
                f"https://www.douyin.com/user/{sec_uid}",
                wait_until="domcontentloaded",
                timeout=60000,
                referer="https://www.google.com/",
            )
            page.wait_for_timeout(6000)
        finally:
            browser.close()

    if not hunter.results:
        raise RuntimeError("未捕获到 aweme/post 接口（可能被风控，稍后重试）")

    items = []
    for data in hunter.json_results():
        for a in data.get("aweme_list") or []:
            vid = a.get("aweme_id")
            if not vid:
                continue
            desc = re.sub(r"\s+", " ", (a.get("desc") or "")).strip()
            items.append({
                "item_id": str(vid),
                "script_id": "douyin",
                "params": {"input": f"https://www.douyin.com/video/{vid}"},
                "title": desc[:60] or "（无文案）",
                "created_at": a.get("create_time") or "",
            })
        if items:
            break  # 只需第一页（最新内容必在首屏）
    return items


def scan(sub: dict):
    """watcher 注册入口：返回 coroutine（在线程中跑同步 playwright）。"""
    import asyncio
    return asyncio.to_thread(_scan_sync, sub)


watcher.register_scanner("douyin", scan)
