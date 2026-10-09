"""Calendar and persistent scheduling tests; no platform/network calls."""
import asyncio
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from app import config, db, scheduling as sch, subscription_store, task_manager, watcher

UTC = dt.timezone.utc
NOW = dt.datetime(2030, 1, 1, tzinfo=UTC)


class CalendarTests(unittest.TestCase):
    def plan(self, **fields):
        return sch.normalize_plan({'kind': 'daily', 'timezone': 'Asia/Shanghai',
                                   'start_date': '2030-01-01', 'times': ['09:00', '18:00'], **fields}, now=NOW)

    def test_daily_multiple_weekdays_and_explicit_dates(self):
        plan = self.plan()
        self.assertEqual(sch.next_occurrence(plan, NOW).isoformat(), '2030-01-01T01:00:00+00:00')
        self.assertEqual(sch.next_occurrence(plan, NOW + dt.timedelta(hours=1)).hour, 10)
        weekly = self.plan(kind='weekly', weekdays=[0, 4])
        self.assertEqual(sch.next_occurrence(weekly, NOW).astimezone(sch.ZoneInfo('Asia/Shanghai')).weekday(), 4)
        dates = self.plan(kind='dates', dates=['2030-01-05', '2030-01-03'])
        self.assertEqual(sch.next_occurrence(dates, NOW).day, 3)

    def test_month_end_anchor_does_not_drift_and_count_expires(self):
        plan = self.plan(kind='repeat', unit='months', every=1, start_date='2030-01-31', times=['09:00'], count=3)
        january = sch.next_occurrence(plan, NOW)
        february = sch.next_occurrence(plan, january)
        march = sch.next_occurrence(plan, february)
        self.assertEqual((january.day, february.day, march.day), (31, 28, 31))
        self.assertIsNone(sch.next_occurrence(plan, march))
        self.assertIsNone(sch.next_occurrence(self.plan(end_date='2030-01-02'), dt.datetime(2030, 1, 3, tzinfo=UTC)))

    def test_every_n_days_weeks_and_dst_gap_fold(self):
        daily = self.plan(kind='repeat', unit='days', every=3, times=['09:00'])
        self.assertEqual(sch.next_occurrence(daily, NOW + dt.timedelta(days=1)).day, 4)
        weekly = self.plan(kind='repeat', unit='weeks', every=2, times=['09:00'])
        self.assertEqual(sch.next_occurrence(weekly, NOW + dt.timedelta(days=1)).day, 15)
        gap = sch.local_instant(dt.date(2026, 3, 8), '02:30', 'America/New_York')
        self.assertEqual(gap.isoformat(), '2026-03-08T07:30:00+00:00')
        fold = sch.local_instant(dt.date(2026, 11, 1), '01:30', 'America/New_York')
        self.assertEqual(fold.isoformat(), '2026-11-01T05:30:00+00:00')

    def test_invalid_fields_rejected(self):
        for fields in ({'timezone': 'Not/A_Zone'}, {'times': ['25:00']}, {'kind': 'weekly', 'weekdays': []},
                       {'kind': 'repeat', 'every': 0}, {'count': -1}, {'end_date': '2029-12-01'}, {'enabled': 'false'}):
            with self.assertRaises(ValueError):
                self.plan(**fields)


class SchedulingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        replacement = patch.object(config, 'DB_PATH', str(Path(self.temp.name) / 'app.db'))
        replacement.start()
        self.addCleanup(replacement.stop)
        for replacement in (patch.object(task_manager, 'broadcast', new=AsyncMock()),
                            patch.object(watcher, '_local_item_ids', return_value=set())):
            replacement.start()
            self.addCleanup(replacement.stop)
        watcher._SCANNING.clear()
        watcher.init_db()
        self.sub = watcher.add_sub('weibo', '123', 'Example', interval_minutes=30)

    async def asyncTearDown(self):
        await watcher.stop_scheduler()

    def fixed(self, **fields):
        return sch.save_schedule(self.sub['id'], {'kind': 'daily', 'timezone': 'Asia/Shanghai',
            'start_date': '2030-01-01', 'times': ['09:00', '18:00'], **fields}, now=NOW)

    def set_interval_due(self, due):
        with db.connect() as conn:
            conn.execute("UPDATE subscription_schedules SET next_due=? WHERE kind='interval'", (sch.stamp(due),))

    async def test_manual_scan_does_not_move_interval_or_fixed_cursor(self):
        self.fixed()
        before = {row['id']: row['next_due'] for row in sch.list_schedules(self.sub['id'])}
        with patch.dict(watcher.SCANNERS, weibo=AsyncMock(return_value=[])):
            await watcher.scan_sub(self.sub)
        after = {row['id']: row['next_due'] for row in sch.list_schedules(self.sub['id'])}
        self.assertEqual(before, after)

    async def test_maintenance_refuses_manual_and_leaves_due_plan_untouched(self):
        self.set_interval_due(NOW)
        with patch.object(config, 'MAINTENANCE_ACTIVE', True, create=True):
            await watcher.scheduler_tick(now=NOW)
            with self.assertRaisesRegex(ValueError, '维护'):
                await watcher.scan_sub(self.sub)
        self.assertEqual(sch.list_runs(self.sub['id']), [])
        self.assertEqual(sch.due_groups(NOW), [self.sub['id']])

    async def test_sleep_coalesces_due_plans_once_with_independent_future_cursors(self):
        self.set_interval_due(NOW)
        fixed = self.fixed()
        resumed = NOW + dt.timedelta(days=3, hours=3)
        self.assertEqual(sch.due_groups(resumed), [self.sub['id']])
        group = sch.claim_group(self.sub['id'], resumed)
        self.assertEqual(len(group['schedules']), 2)
        self.assertIsNone(sch.claim_group(self.sub['id'], resumed))
        sch.finish_group(group['id'], {'scan_id': 'scan1'}, now=resumed)
        plans = {row['id']: row for row in sch.list_schedules(self.sub['id'])}
        self.assertEqual(sch.utc(plans[f"interval-{self.sub['id']}"]['next_due']), resumed + dt.timedelta(minutes=30))
        self.assertEqual(sch.utc(plans[fixed['id']]['next_due']).hour, 10)
        self.assertEqual(sch.due_groups(resumed), [])
        self.assertEqual(len(sch.list_runs(self.sub['id'])), 1)

    async def test_failed_retry_is_bounded_and_never_postpones_normal_due(self):
        self.set_interval_due(NOW)
        group = sch.claim_group(self.sub['id'], NOW)
        normal_due = sch.list_schedules(self.sub['id'])[0]['next_due']
        current = NOW
        for attempt in range(4):
            sch.finish_group(group['id'], {'error': 'network unavailable'}, now=current)
            row = sch.list_schedules(self.sub['id'])[0]
            self.assertEqual(row['next_due'], normal_due)
            if attempt < 3:
                self.assertEqual(sch.utc(row['retry_due']) - current, dt.timedelta(seconds=30 * 2 ** attempt))
                current = sch.utc(row['retry_due'])
                group = sch.claim_group(self.sub['id'], current)
            else:
                self.assertIsNone(row['retry_due'])

    async def test_restart_recovers_claimed_run_and_edit_revision_wins(self):
        self.set_interval_due(NOW)
        group = sch.claim_group(self.sub['id'], NOW)
        sch.recover_runs(NOW + dt.timedelta(seconds=10))
        self.assertEqual(sch.list_runs(self.sub['id'])[0]['status'], 'interrupted')
        self.assertEqual(sch.due_groups(NOW + dt.timedelta(seconds=10)), [self.sub['id']])
        group = sch.claim_group(self.sub['id'], NOW + dt.timedelta(seconds=10))
        updated = sch.save_schedule(self.sub['id'], {'kind': 'interval', 'minutes': 60, 'enabled': False}, now=NOW)
        sch.finish_group(group['id'], {'error': 'late error'}, now=NOW)
        row = sch.list_schedules(self.sub['id'])[0]
        self.assertEqual(row['next_due'], updated['next_due'])
        self.assertIsNone(row['retry_due'])
        self.assertFalse(row['enabled'])

    async def test_invalid_edits_do_not_partially_commit_or_replace_interval(self):
        fixed = self.fixed()
        with self.assertRaises(ValueError):
            watcher.update_sub(self.sub['id'], interval_minutes=-1)
        self.assertEqual(watcher.list_subs()[0]['interval_minutes'], 30)
        with self.assertRaises(ValueError):
            watcher.add_sub('weibo', 'invalid', interval_minutes=9999999)
        self.assertEqual(len(watcher.list_subs()), 1)
        with self.assertRaises(ValueError):
            sch.save_schedule(self.sub['id'], {'kind': 'interval', 'minutes': 60}, fixed['id'], now=NOW)
        with self.assertRaises(ValueError):
            sch.save_schedule(self.sub['id'], fixed['plan'], f"interval-{self.sub['id']}", now=NOW)
        self.assertEqual(len(sch.list_schedules(self.sub['id'])), 2)

    async def test_oldest_due_is_fair_and_disabled_sub_cannot_be_claimed(self):
        other = watcher.add_sub('weibo', 'older')
        self.set_interval_due(NOW)
        with db.connect() as conn:
            conn.execute('UPDATE subscription_schedules SET next_due=? WHERE sub_id=?',
                         (sch.stamp(NOW - dt.timedelta(days=1)), other['id']))
        self.assertEqual(sch.due_groups(NOW, limit=1), [other['id']])
        watcher.update_sub(other['id'], enabled=False)
        self.assertIsNone(sch.claim_group(other['id'], NOW))
        self.assertEqual(sch.due_groups(NOW), [self.sub['id']])

    async def test_global_concurrency_bound_and_overlap_reuses_manual_result(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def scanner(_sub):
            entered.set()
            await release.wait()
            return []
        with patch.dict(watcher.SCANNERS, weibo=scanner):
            manual = asyncio.create_task(watcher.scan_sub(self.sub))
            await entered.wait()
            self.set_interval_due(NOW)
            await watcher.scheduler_tick(now=NOW)
            await asyncio.sleep(0)
            self.assertEqual(len(watcher._SCANNING), 1)
            release.set()
            await manual
            await asyncio.gather(*list(watcher._scheduled_jobs.values()))
        self.assertEqual(len(watcher.list_scans(self.sub['id'])), 1)
        self.assertEqual(sch.list_runs(self.sub['id'])[0]['status'], 'success')

    async def test_scan_concurrency_is_bounded_across_different_subscriptions(self):
        subs = [self.sub, watcher.add_sub('weibo', '456'), watcher.add_sub('weibo', '789')]
        occupied, release = asyncio.Event(), asyncio.Event()
        active, peak = 0, 0
        async def scanner(_sub):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            if active == 2:
                occupied.set()
            try:
                await release.wait()
                return []
            finally:
                active -= 1
        with patch.object(config, 'MAX_SCAN_CONCURRENCY', 2, create=True), patch.dict(watcher.SCANNERS, weibo=scanner):
            jobs = [asyncio.create_task(watcher.scan_sub(sub)) for sub in subs]
            try:
                await asyncio.wait_for(occupied.wait(), timeout=2)
                await asyncio.sleep(0)
                self.assertEqual(active, 2)
                self.assertEqual(len(watcher._SCANNING), 3)
                self.assertTrue(watcher.has_active_scans())
            finally:
                release.set()
                await asyncio.gather(*jobs)
        self.assertEqual(peak, 2)
        self.assertFalse(watcher.has_active_scans())

    async def test_incomplete_coverage_keeps_prior_watermark_and_private_avatar(self):
        sch.record_coverage(self.sub['id'], {'complete': True}, now=NOW)
        before = sch.scan_options(self.sub['id'])['cutoff_at']
        sch.record_coverage(self.sub['id'], {'complete': False, 'reason': 'page_limit'}, now=NOW + dt.timedelta(days=1))
        self.assertEqual(sch.scan_options(self.sub['id'])['cutoff_at'], before)
        with db.connect() as conn:
            conn.execute('UPDATE subscriptions SET avatar_source=? WHERE id=?', ('https://cdn.invalid/a?token=private', self.sub['id']))
        row = watcher.list_subs()[0]
        self.assertNotIn('avatar_source', row)
        self.assertEqual(row['avatar_url'], f"/api/subs/{self.sub['id']}/avatar")

    async def test_partial_schedule_keeps_coverage_reason_without_network_retry(self):
        self.set_interval_due(NOW)
        group = sch.claim_group(self.sub['id'], NOW)
        scan_id = subscription_store.begin_scan(self.sub, 'scheduled')
        coverage = {'complete': False, 'reason': 'page_limit', 'pages': 5, 'items': 100}
        subscription_store.finish_scan(scan_id, 'success', {'coverage': coverage})
        sch.finish_group(group['id'], {'scan_id': scan_id, 'coverage': coverage}, now=NOW)
        row = sch.list_runs(self.sub['id'])[0]
        self.assertEqual(row['status'], 'partial')
        self.assertIn('页数上限', row['error'])
        self.assertEqual(row['coverage'], coverage)
        self.assertIsNone(sch.list_schedules(self.sub['id'])[0]['retry_due'])


if __name__ == '__main__':
    unittest.main()
