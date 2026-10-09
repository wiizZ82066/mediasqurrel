"""Bounded, cancellable response collection shared by the platform scanners."""
import asyncio
import datetime as dt
import email.utils
import re
import threading
import time

from ..redaction import redact_text
from ..scheduling import DEFAULT_LIMITS, utc, stamp


def published_at(value):
    try:
        if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
            return stamp(dt.datetime.fromtimestamp(float(value), dt.timezone.utc))
        if value:
            try:
                return stamp(utc(str(value)))
            except ValueError:
                return stamp(email.utils.parsedate_to_datetime(str(value)))
    except (ValueError, TypeError, OverflowError):
        pass
    return None


class ScanCancelled(Exception):
    pass


class Collector:
    def __init__(self, sub, parser):
        self.options = {**DEFAULT_LIMITS, **sub.get('scan_options', {})}
        self.cancel = sub.get('_cancel_event') or threading.Event()
        self.parser = parser
        self.started = time.monotonic()
        self.items, self.signatures = {}, set()
        self.pages, self.complete, self.reason = 0, False, ''
        self.oldest, self.valid, self.error = None, False, None
        self.avatar_source = None

    def remaining(self):
        return self.options['timeout_seconds'] - (time.monotonic() - self.started)

    def check(self):
        if self.cancel.is_set():
            raise ScanCancelled('扫描已取消')
        if self.error:
            raise RuntimeError(self.error)

    def accept(self, payload, *, status=200):
        if self.pages >= self.options['max_pages'] or self.reason or self.cancel.is_set():
            return
        if status != 200:
            self.error = f'平台列表请求失败（HTTP {status}）；可能限流或需要登录'
            return
        try:
            items, has_more = self.parser(payload)
        except (ValueError, TypeError, KeyError) as error:
            self.error = redact_text(str(error))
            return
        signature = (tuple(item['item_id'] for item in items), has_more)
        if signature in self.signatures:
            return
        self.signatures.add(signature)
        self.valid, self.pages = True, self.pages + 1
        for item in items:
            item = dict(item)
            self.avatar_source = item.pop('_avatar_source', None) or self.avatar_source
            self.items.setdefault(item['item_id'], item)
            if not item.get('pinned') and item.get('created_at'):
                self.oldest = min(self.oldest, item['created_at']) if self.oldest else item['created_at']
            if len(self.items) >= self.options['max_items']:
                self.reason = 'item_limit'
                break
        if self.reason:
            return
        if self.options.get('baseline'):
            self.complete, self.reason = True, 'baseline_window'
        elif has_more is False:
            self.complete, self.reason = True, 'platform_end'
        elif self.oldest and self.options.get('cutoff_at') and utc(self.oldest) <= utc(self.options['cutoff_at']):
            self.complete, self.reason = True, 'overlap_covered'
        elif self.pages >= self.options['max_pages']:
            self.reason = 'page_limit'

    def result(self):
        self.check()
        if not self.valid:
            raise RuntimeError('未获取有效内容列表；请检查网络、登录状态或平台验证')
        reason = self.reason or ('time_limit' if self.remaining() <= 0 else 'pagination_stalled')
        return {'items': list(self.items.values()), 'avatar_source': self.avatar_source, 'coverage': {
            'complete': self.complete, 'reason': reason, 'pages': self.pages,
            'items': len(self.items), 'oldest_at': self.oldest, 'cutoff_at': self.options.get('cutoff_at'),
            'elapsed_seconds': round(time.monotonic() - self.started, 3),
            'scope': 'baseline_current_page' if self.options.get('baseline') else 'overlap_window'}}


def collect_browser(sub, parser, launch, setup_context, url, url_pattern):
    collector = Collector(sub, parser)
    try:
        from ..browser import sync_playwright
        with sync_playwright() as playwright:
            collector.check()
            browser = launch(playwright)
            try:
                context = setup_context(browser)
                page = context.new_page()
                page.set_default_timeout(2000)
                pattern = re.compile(url_pattern)
                def on_response(response):
                    if not pattern.search(response.url) or collector.reason or collector.cancel.is_set():
                        return
                    try:
                        collector.accept(response.json(), status=response.status)
                    except Exception as error:
                        collector.error = '列表响应无法解析: ' + redact_text(str(error))
                page.on('response', on_response)
                page.goto(url, wait_until='domcontentloaded', timeout=max(1, min(10000, int(collector.remaining() * 1000))))
                unchanged = 0
                while collector.remaining() > 0 and not collector.reason:
                    collector.check()
                    previous = collector.pages
                    for _ in range(12):
                        collector.check()
                        if collector.reason or collector.remaining() <= 0:
                            break
                        page.wait_for_timeout(min(250, max(1, int(collector.remaining() * 1000))))
                    collector.check()
                    if collector.reason:
                        break
                    unchanged = unchanged + 1 if collector.pages == previous else 0
                    if unchanged >= 3:
                        break
                    page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
                return collector.result()
            finally:
                browser.close()
    except ScanCancelled:
        raise
    except Exception as error:
        raise RuntimeError('平台扫描失败: ' + redact_text(str(error))) from error


async def run_sync_scan(function, sub):
    cancel = sub.get('_cancel_event') or threading.Event()
    def invoke():
        try:
            return True, function({**sub, '_cancel_event': cancel})
        except Exception as error:
            # A shield abandoned by cancellation must never own an unobserved
            # exception. Deliver errors as values until the awaiting owner resumes.
            return False, error
    worker = asyncio.create_task(asyncio.to_thread(invoke))
    try:
        succeeded, result = await asyncio.shield(worker)
    except asyncio.CancelledError:
        cancel.set()
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                cancel.set()
        raise
    if not succeeded:
        raise result
    return result
