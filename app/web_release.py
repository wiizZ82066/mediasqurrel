"""Switch compatible web generations without replacing the owner of tasks, recordings or SQLite."""
import asyncio
from dataclasses import dataclass
import hmac
from importlib import import_module
import json
from pathlib import Path
import secrets
import sqlite3
import subprocess
import tempfile

from fastapi import HTTPException

from . import config, runtime, web_api, web_bundle
from .archive import atomic_write_text
from .web_worker import WebWorker, WebWorkerError, check_contract

manager = None


def directory(source_id):
    return Path(config.DATA_DIR) / 'web-releases' / source_id


def check_core():
    """Executed in the candidate interpreter, without starting its scheduler or writing its database."""
    from contextlib import closing
    from . import db
    import_module('app.main')  # Import every candidate route without running lifespan.
    identity = runtime.release_identity()
    bundle = web_bundle.stage(directory(identity['source_id']), Path(config.SOURCE_DIR), identity)
    probe = Generation.open(bundle)
    probe.worker.close()
    database = Path(config.DB_PATH).resolve()
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as connection:
        version = connection.execute('PRAGMA user_version').fetchone()[0]
    target = max(step[0] for step in db._migrations())
    if version > target:
        raise ValueError('候选程序无法读取当前数据库版本')
    if version < target:
        with tempfile.TemporaryDirectory(prefix='schema-check-', dir=config.DATA_DIR) as folder:
            copy = Path(folder) / 'app.db'
            db.backup_database(copy, source=database)
            if db.migrate(copy) != target:
                raise RuntimeError('数据库副本升级验证失败')


async def snapshot(name):
    if name == 'home':
        from . import home_api
        return await home_api.web_data()
    if name == 'activity':
        from . import activity, live_service
        return {'counts': activity.counts(), 'recordings': live_service.active_recordings()}
    from . import script_registry
    return {'scripts': [{k: v for k, v in item.items() if not k.startswith('_')} | {'available': item['_available']}
                        for item in script_registry.all_scripts()]}


def busy_reason():
    from . import (library_service, live_service, mail_delivery, settings_api, sub_search,
                   subscription_batch, subscription_standby, task_manager, watcher)
    if live_service.busy():
        return '正在检查直播、录制或整理录播'
    if task_manager.update_state()['active'] or task_manager._jobs or task_manager._archive_jobs:
        return '下载任务或归档正在进行'
    if watcher.has_active_scans() or watcher._scheduled_jobs or subscription_batch.snapshot()['status'] == 'running':
        return '订阅扫描正在进行'
    if library_service._scans or settings_api._work or config.MAINTENANCE_ACTIVE:
        return '媒体库或数据库正在维护'
    if subscription_standby._copy_job is not None and not subscription_standby._copy_job.done():
        return '正在同步媒体盘'
    if mail_delivery.busy():
        return '正在处理下载或录播邮件'
    from .weibo_auth import _LOGIN_LOCK
    if _LOGIN_LOCK.locked() or any(state['requests'] for state in sub_search._ASYNC_SEARCH.values()):
        return '平台登录或搜索正在进行'
    return ''


@dataclass
class Generation:
    bundle: web_bundle.Bundle
    worker: WebWorker
    readers: int = 0
    retired: bool = False

    @classmethod
    def open(cls, bundle):
        worker = WebWorker(bundle.code, bundle.api_hash)
        try:
            check_contract(worker)
        except BaseException:
            worker.close()
            raise
        return cls(bundle, worker)


class Releases:
    def __init__(self, source, identity, base):
        self.source, self.boot, self.base = source, identity, base
        self.active = None
        self.previous = ''
        self.requested = ''
        self.lock = asyncio.Lock()
        self.token = secrets.token_urlsafe(32)
        self.state, self.message = 'ready', ''
        self.waiting = None
        self.closing = False
        self.finishing = set()
        self.generations = {}

    def authorize(self, supplied):
        return hmac.compare_digest(self.token, supplied) if supplied.isascii() else False

    def status(self):
        return {'state': self.state, 'message': self.message, 'web': self.active.bundle.version if self.active else '',
                'previous': self.previous}

    def health(self):
        return {'publish_protocol': 1, 'code_signature': runtime.combined_signature(self.boot['core'], self.active.bundle.version),
                'requested_signature': runtime.combined_signature(self.boot['core'], self.requested)}

    def save(self, current, previous, requested):
        atomic_write_text(self.base / 'current.json', json.dumps({
            'core': self.boot['core'], 'active': current, 'previous': previous, 'requested': requested}))

    async def open(self, version):
        bundle = await asyncio.to_thread(web_bundle.load, self.base, version)
        return await asyncio.to_thread(Generation.open, bundle)

    async def stage(self, identity):
        bundle = await asyncio.to_thread(web_bundle.stage, self.base, self.source, identity)
        return await asyncio.to_thread(Generation.open, bundle)

    async def start(self):
        self.base.mkdir(parents=True, exist_ok=True)
        path = self.base / 'current.json'
        record = json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {}
        same_core = record.get('core') == self.boot['core']
        try:
            if same_core and record.get('requested') == self.boot['web']:
                self.active = await self.open(record['active'])
                self.previous = record.get('previous', '')
                if self.active.bundle.version != self.boot['web']:
                    self.state, self.message = 'rolled_back', '当前使用上一个已验证的网页版本。'
            else:
                self.active = await self.stage(self.boot)
                self.previous = record['active'] if same_core else ''
        except Exception:
            if not same_core:
                raise
            # A bad candidate or damaged current bundle can never start a second task owner.
            for version in dict.fromkeys(filter(None, (record.get('active'), record.get('previous')))):
                try:
                    self.active = await self.open(version)
                    break
                except (ValueError, OSError, KeyError, RuntimeError):
                    continue
            if self.active is None:
                raise RuntimeError('没有可启动的已验证网页版本') from None
            self.state, self.message = 'rolled_back', '网页校验失败，已恢复上一个可用版本。'
        self.requested = self.boot['web']
        self.generations[self.active.bundle.version] = self.active
        self.save(self.active.bundle.version, self.previous, self.requested)
        atomic_write_text(self.base / 'launcher.key', self.token)
        (self.base / 'launcher.key').chmod(0o600)

    def track(self, task):
        self.finishing.add(task)

        def finished(done):
            self.finishing.discard(done)
            if not done.cancelled():
                done.exception()  # A browser may have disconnected while the worker was finishing.

        task.add_done_callback(finished)
        return task

    def retire(self, generation):
        if generation is not self.active and not generation.readers and not generation.retired:
            generation.retired = True
            if self.generations.get(generation.bundle.version) is generation:
                self.generations.pop(generation.bundle.version)
            self.track(asyncio.create_task(asyncio.to_thread(generation.worker.close)))

    def switch(self, candidate, requested):
        old = self.active
        try:
            self.save(candidate.bundle.version, old.bundle.version, requested)
        except BaseException:
            self.retire(candidate)
            raise
        pinned = self.generations.get(candidate.bundle.version)
        if pinned and pinned.worker.process.is_alive():
            self.retire(candidate)
            candidate = pinned
        self.active, self.previous, self.requested = candidate, old.bundle.version, requested
        self.generations[candidate.bundle.version] = candidate
        self.retire(old)

    async def rollback(self, failed=None):
        async with self.lock:
            if failed is not None and self.active is not failed:
                return
            if self.waiting or self.closing:
                raise ValueError('后台即将切换，请稍后重试')
            if failed is not None and not self.previous:
                replacement = await self.open(self.active.bundle.version)
                self.active = replacement
                self.generations[replacement.bundle.version] = replacement
                self.retire(failed)
                return
            if not self.previous or self.previous == self.active.bundle.version:
                raise ValueError('当前没有可切回的兼容网页版本')
            previous = await self.open(self.previous)
            self.switch(previous, self.requested)
            self.state, self.message = 'rolled_back', '已切回上一版网页和接口，刷新页面即可使用。'

    async def execute(self, generation, path):
        try:
            source = generation.worker.routes.get(path)
            if source is None:
                raise HTTPException(404, '网页接口不存在')
            data = await snapshot(source)
            return await asyncio.to_thread(generation.worker.call, path, data)
        finally:
            self.unpin(generation)

    async def pin(self, version):
        generation = self.generations.get(version) if version else self.active
        if not generation and version == self.previous:
            async with self.lock:
                generation = self.generations.get(version)
                if generation is None and version == self.previous:
                    generation = await self.open(version)
                    self.generations[version] = generation
        if not generation:
            raise HTTPException(409, '当前标签页版本已过期，请刷新页面')
        generation.readers += 1
        return generation

    def unpin(self, generation):
        generation.readers -= 1
        self.retire(generation)

    async def render(self, path, version=''):
        for attempt in range(2):
            generation = await self.pin(version)
            try:
                # A disconnected browser must not retire the worker while its call finishes.
                return await asyncio.shield(self.track(asyncio.create_task(self.execute(generation, path))))
            except WebWorkerError as error:
                if attempt or self.closing or generation is not self.active:
                    raise HTTPException(503, '网页接口暂时不可用') from error
                try:
                    await self.rollback(generation)
                    version = ''
                except (ValueError, OSError, RuntimeError):
                    raise HTTPException(503, '网页接口暂时不可用，请重新发布') from error

    async def publish(self, expected):
        async with self.lock:
            if self.closing:
                raise ValueError('后台正在退出')
            identity = await asyncio.to_thread(runtime.release_identity, self.source)
            if expected != identity['code_signature']:
                raise ValueError('源码已变化，请重新运行发布命令')
            if self.waiting:
                self.waiting.cancel()
                await asyncio.gather(self.waiting, return_exceptions=True)
                self.waiting = None
            self.state, self.message = 'publishing', '正在验证网页和接口。'
            try:
                if identity['core'] != self.boot['core']:
                    # A new core can introduce a new web protocol; validate that pair together.
                    from .windows_runtime import console_python
                    await asyncio.to_thread(subprocess.run, [console_python(), '-B', '-c', 'from app.web_release import check_core; check_core()'],
                                            cwd=self.source, check=True, timeout=180, stdout=subprocess.DEVNULL,
                                            stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                    self.state, self.message = 'waiting', busy_reason() or '正在准备切换后台。'
                    self.waiting = asyncio.create_task(self.restart_when_idle(identity))
                else:
                    candidate = await self.stage(identity)
                    try:
                        if (await asyncio.to_thread(runtime.release_identity, self.source)) != identity:
                            raise ValueError('验证期间源码发生变化，请重新发布')
                        if self.active.bundle.version != identity['web']:
                            self.switch(candidate, identity['web'])
                        self.state, self.message = 'ready', '网页和接口已就绪，刷新页面即可使用。'
                    finally:
                        self.retire(candidate)
            except Exception as error:
                # Expected contract and integrity messages contain no user data or launch arguments.
                reason = str(error) if isinstance(error, (ValueError, RuntimeError)) else ''
                if len(reason) > 160 or not reason.startswith(('网页', '构建', '前端', '同名', '验证期间')):
                    reason = '候选后台校验失败' if identity['core'] != self.boot['core'] else '网页版本校验失败'
                self.state, self.message = 'failed', reason + '。当前版本继续服务。'
                raise

    async def restart_when_idle(self, identity):
        try:
            # Allow the launcher to receive the acceptance before initiating a graceful exit.
            await asyncio.sleep(1)
            while not self.closing:
                self.message = busy_reason()
                if not self.message:
                    if (await asyncio.to_thread(runtime.release_identity, self.source)) != identity:
                        raise ValueError('等待期间源码已变化，请重新发布。')
                    self.message = busy_reason()
                    if not self.message:
                        if not runtime.restart_handler:
                            raise ValueError('当前启动方式不支持后台切换。')
                        config.MAINTENANCE_ACTIVE = True
                        self.message = '正在正常切换后台，网页将自动重连。'
                        runtime.restart_handler()
                        return
                await asyncio.sleep(2)
        except Exception:
            self.state, self.message = 'failed', '后台切换已取消，请检查源码并重新发布。'
        finally:
            self.waiting = None

    async def stop(self):
        self.closing = True
        if self.waiting:
            self.waiting.cancel()
            await asyncio.gather(self.waiting, return_exceptions=True)
        while self.finishing:
            await asyncio.gather(*list(self.finishing), return_exceptions=True)
        workers = list({id(g): g for g in [*self.generations.values(), self.active] if g}.values())
        for generation in workers:
            await asyncio.to_thread(generation.worker.close)
        self.generations.clear()


async def start(expected):
    global manager
    if not runtime.web_publishing:
        return
    identity = await asyncio.to_thread(runtime.release_identity)
    if expected['code_signature'] != identity['code_signature']:
        raise RuntimeError('后台启动期间源码已变化，请重新启动')
    service = Releases(Path(config.SOURCE_DIR), identity, directory(identity['source_id']))
    try:
        await service.start()
    except BaseException:
        await service.stop()
        raise
    manager = service


async def stop():
    global manager
    if manager:
        await manager.stop()
        manager = None


async def render(path, version=''):
    if manager:
        return await manager.render(path, version)
    if path not in web_api.ROUTES:
        raise HTTPException(404, '网页接口不存在')
    return web_api.render(path, await snapshot(web_api.ROUTES[path]))
