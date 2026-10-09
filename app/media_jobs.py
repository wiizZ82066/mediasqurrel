"""Bounded, deduplicated background media work, separate from metadata queries."""
import asyncio
import itertools


class BusyError(RuntimeError):
    pass


class MediaJobs:
    def __init__(self, workers=2, capacity=64):
        self.workers = workers
        self.capacity = capacity
        self.queue = asyncio.PriorityQueue(maxsize=capacity)
        self.pending = {}
        self.tasks = []
        self.sequence = itertools.count()
        self.closing = False

    def start(self):
        if not self.tasks:
            self.tasks = [asyncio.create_task(self._worker()) for _ in range(self.workers)]

    def submit(self, key, function, *args, priority=10):
        if key in self.pending:
            return self.pending[key]
        if priority >= 10 and len(self.pending) >= self.capacity // 2:
            raise BusyError('后台分析暂缓，优先处理正在查看的缩略图')
        if self.closing or len(self.pending) >= self.capacity:
            raise BusyError('图像处理队列已满，请稍后重试')
        self.start()
        future = asyncio.get_running_loop().create_future()
        # Background cover callers may not await this result; retrieve failures
        # to prevent unhandled-future warnings while preserving await semantics.
        future.add_done_callback(lambda result: result.exception() if not result.cancelled() else None)
        self.pending[key] = future
        self.queue.put_nowait((priority, next(self.sequence), key, function, args, future))
        return future

    async def _worker(self):
        while True:
            _priority, _number, key, function, args, future = await self.queue.get()
            try:
                if function is None:
                    return
                if future.cancelled():
                    continue
                try:
                    value = await asyncio.to_thread(function, *args)
                    if not future.done():
                        future.set_result(value)
                except Exception as error:
                    if not future.done():
                        future.set_exception(error)
            finally:
                self.pending.pop(key, None)
                self.queue.task_done()

    async def close(self):
        self.closing = True
        # Discard waiting cache work. Source media and durable catalog work are
        # not owned by this queue, and remain untouched.
        while not self.queue.empty():
            _priority, _number, key, _function, _args, future = self.queue.get_nowait()
            self.pending.pop(key, None)
            future.cancel()
            self.queue.task_done()
        for _task in self.tasks:
            await self.queue.put((100, next(self.sequence), None, None, (), None))
        await asyncio.gather(*self.tasks)
        self.tasks.clear()


jobs = None


def start():
    global jobs
    jobs = MediaJobs()
    jobs.start()
    return jobs


async def stop():
    global jobs
    if jobs is not None:
        await jobs.close()
        jobs = None
