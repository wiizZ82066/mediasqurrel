import asyncio
import json
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch, AsyncMock
from app import config, db, live_api, live_douyin, live_scheduling, live_service, live_store, scheduling, watcher


class ProfileTests(unittest.TestCase):
    def test_profile_requires_exact_identity_explicit_status_and_safe_room(self):
        user=dict(sec_uid='owner',live_status=1,room_data=json.dumps({'owner':{'web_rid':'123456'}}))
        self.assertEqual(live_douyin.parse_profile({'status_code':0,'user':user},'owner'),'https://live.douyin.com/123456')
        self.assertIsNone(live_douyin.parse_profile({'status_code':0,'user':{**user,'live_status':0}},'owner'))
        for value in ({**user,'sec_uid':'other'}, {**user,'live_status':None}, {**user,'room_data':'{}'}):
            with self.assertRaises(live_douyin.LiveError):
                live_douyin.parse_profile({'status_code':0,'user':value},'owner')


class CalendarTests(unittest.TestCase):
    def test_independent_cursors_manual_check_missed_coalescing_and_end(self):
        plan=scheduling.normalize_plan(dict(kind='daily',start_date='2026-10-18',times=['09:00','18:00'],timezone='Asia/Shanghai'))
        monitor=dict(interval_seconds=3600,interval_enabled=True,plan=plan)
        timing=live_scheduling.cursors(monitor,initial=True,now='2026-10-18T00:00:00Z')
        monitor.update(timing)
        monitor.update(live_scheduling.cursors(monitor,now='2026-10-18T00:00:00Z'))
        unchanged=live_scheduling.cursors(monitor,now='2026-10-18T00:30:00Z')
        self.assertEqual(unchanged['next_interval_at'],monitor['next_interval_at'])
        self.assertEqual(unchanged['next_plan_at'],monitor['next_plan_at'])
        coalesced=live_scheduling.cursors(monitor,now='2026-10-20T12:00:00Z')
        self.assertEqual(coalesced['next_interval_at'],'2026-10-20T13:00:00.000+00:00')
        self.assertEqual(coalesced['next_plan_at'],'2026-10-21T01:00:00.000+00:00')
        monitor.update(interval_enabled=False,plan={**plan,'count':1},next_plan_at='2026-10-18T01:00:00.000+00:00')
        done=live_scheduling.cursors(monitor,now='2026-10-20T12:00:00Z')
        self.assertEqual(done['next_check_at'],'')
        self.assertEqual(live_scheduling.cursors({**monitor,**done},now='2026-10-20T12:01:00Z')['next_check_at'],'')
        self.assertTrue(live_scheduling.cursors(monitor,now='2026-10-20T12:00:00Z',active=True)['next_check_at'])

    def test_v8_upgrade_preserves_existing_link_and_due(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'test.db'
            db.migrate(path,migrations=[s for s in db._migrations() if s[0]<=8])
            with db.connect(path) as c:
                # Existing subscription schema requires these public fields only.
                c.execute("INSERT INTO subscriptions(platform,blogger_id,nickname) VALUES('douyin','fixture','示例')")
                c.execute("INSERT INTO live_monitors(sub_id,room_url,enabled,next_check_at) VALUES(1,'https://live.douyin.com/123',1,'2026-10-18T01:00:00+00:00')")
            db.migrate(path)
            with db.connect(path) as c:
                item=dict(c.execute('SELECT * FROM live_monitors').fetchone())
            self.assertEqual(item['source'],'room')
            self.assertEqual(item['next_interval_at'],item['next_check_at'])
            self.assertTrue(list((Path(folder)/'backups').glob('schema-v8-*.db')))


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_profile_monitor_uses_identity_without_configured_room_and_preserves_plan(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(config,'DB_PATH',str(Path(folder)/'test.db')), patch.object(config,'MAINTENANCE_ACTIVE',False):
            watcher.init_db()
            sub=watcher.add_sub('douyin','fixture-owner','示例')
            plan=dict(kind='weekly',timezone='Asia/Shanghai',start_date='2026-10-18',times=['09:00','18:00'],weekdays=[0,2,4])
            saved=await live_api.configure(sub['id'],dict(enabled=True,interval_seconds=300,source='profile',interval_enabled=False,plan=plan,quality='hd'))
            self.assertEqual(saved['room_url'],'')
            self.assertFalse(saved['interval_enabled'])
            self.assertEqual(saved['plan']['times'],['09:00','18:00'])
            self.assertEqual(saved['quality'],'hd')
            info=dict(live=True,owner_id='fixture-owner',session_id='123',title='示例',stream_url='https://test.douyincdn.com/a.flv',room_url='https://live.douyin.com/123')
            live_service._stopping=False
            async def record(*args):pass
            try:
                with patch.object(live_douyin,'lookup_blogger',return_value=info) as lookup, patch.object(live_service,'_record',side_effect=record), patch('shutil.disk_usage') as space:
                    space.return_value.free=10*1024**3
                    await live_service.check(sub['id'])
                    lookup.assert_called_once_with('fixture-owner', quality='hd')
                    owner=live_service._active[sub['id']]
                    await owner['task']
                    row=live_store.recordings(sub['id'])[0]
                    self.assertEqual(row['room_url'],'https://live.douyin.com/123')
                    self.assertTrue(live_store.monitor(sub['id'])['next_check_at'])
            finally:
                live_service._active.clear()
