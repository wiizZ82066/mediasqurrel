"""抖音博主扫描器：检测博主主页新视频。

技术路线（已实测可行）:
  scrapling DynamicFetcher (real_chrome 无头) 打开
  https://www.douyin.com/user/{sec_uid}，
  拦截主页自带的视频列表接口 aweme/v1/web/aweme/post 响应。

订阅标识: blogger_id 使用 sec_uid（长串 MS4wLjAB...）。
  获取方式: 视频页 aweme_detail 的 author.sec_uid，
  新下载的存档 context.md 里已自动记录。

去重: watcher 层用 seen_items 表按 aweme_id 记录；首次扫描只对齐基线。
注意: 主页列表可能含置顶旧视频，去重不依赖顺序，无影响。
"""
import asyncio
import json
import re

from scrapling import DynamicFetcher

from .. import watcher


def _scan_sync(sub: dict) -> list[dict]:
    sec_uid = str(sub["blogger_id"]).strip()

    page = DynamicFetcher.fetch(
        f"https://www.douyin.com/user/{sec_uid}",
        real_chrome=True,
        headless=True,
        capture_xhr=r"aweme/v1/web/aweme/post",
        wait=6000,
        load_dom=True,
        timeout=60000,
    )

    items = []
    for xr in page.captured_xhr:
        body = xr.body
        if isinstance(body, bytes):
            body = body.decode("utf-8", errors="replace")
        try:
            data = json.loads(body)
        except Exception:
            continue
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

    if not page.captured_xhr:
        raise RuntimeError("未捕获到 aweme/post 接口（可能被风控，稍后重试）")
    return items


def scan(sub: dict):
    """watcher 注册入口：返回 coroutine（在线程中跑 scrapling 同步 API）。"""
    return asyncio.to_thread(_scan_sync, sub)


watcher.register_scanner("douyin", scan)
