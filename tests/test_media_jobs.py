import asyncio
import threading
import unittest

from app.media_jobs import BusyError, MediaJobs


class MediaJobTests(unittest.IsolatedAsyncioTestCase):
    async def test_bounded_queue_deduplicates_and_waits_for_worker_cleanup(self):
        jobs = MediaJobs(workers=2, capacity=4)
        released = threading.Event()
        lock = threading.Lock()
        active = peak = calls = 0

        def work():
            nonlocal active, peak, calls
            with lock:
                active += 1
                calls += 1
                peak = max(peak, active)
            released.wait(5)
            with lock:
                active -= 1
            return 'ready'

        futures = [jobs.submit(str(index), work, priority=0) for index in range(4)]
        self.assertIs(jobs.submit('0', work), futures[0])
        with self.assertRaises(BusyError):
            jobs.submit('overflow', work)
        await asyncio.sleep(.05)
        self.assertEqual(peak, 2)
        closing = asyncio.create_task(jobs.close())
        await asyncio.sleep(.02)
        self.assertFalse(closing.done())
        released.set()
        await asyncio.wait_for(closing, 3)
        self.assertEqual(calls, 2)
        self.assertEqual(active, 0)
        self.assertTrue(futures[2].cancelled())
        self.assertEqual(jobs.pending, {})


if __name__ == '__main__':
    unittest.main()
