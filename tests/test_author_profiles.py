from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app import author_profiles, avatars, config


class AuthorProfileTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        for key, value in [('CACHE_DIR',temporary.name),('MAINTENANCE_ACTIVE',False)]:
            replacement = patch.object(config,key,value)
            replacement.start()
            self.addCleanup(replacement.stop)
        author_profiles._failed_until.clear()

    def test_profile_requires_exact_identity_and_approved_avatar_host(self):
        identity='MS4wLjABAAAAfixtureprofile'
        profile={'secUid':identity,'nickname':'Example','avatarThumb':{'urlList':['https://p3.douyinpic.com/a.jpg']}}
        self.assertIsNotNone(author_profiles._douyin_profile(profile,identity))
        self.assertIsNone(author_profiles._douyin_profile(profile,identity+'other'))
        profile['avatarThumb']['urlList']=['https://127.0.0.1/secret']
        self.assertIsNone(author_profiles._douyin_profile(profile,identity))
        for identity in ('../secret','https://example.invalid','?token=x'):
            self.assertIsNone(author_profiles.avatar_url('douyin',identity))
        self.assertEqual(author_profiles.from_metadata('weibo',{'原文链接':'https://weibo.com/123/post'}),'123')
        self.assertEqual(author_profiles.from_metadata('weibo',{'原文链接':'https://weibo.com.attacker.invalid/123/post'}),'')

    def test_cached_avatar_does_not_repeat_profile_lookup_or_store_signed_source(self):
        with patch.object(author_profiles,'fetch_profile',return_value={'avatar_source':'https://tvax1.sinaimg.cn/a?auth_key=private'}) as profile, \
             patch.object(avatars,'_download',return_value=b'normalized fixture image') as download:
            first=author_profiles.get_avatar('weibo','123')
            self.assertEqual(author_profiles.get_avatar('weibo','123'),first)
            self.assertEqual(profile.call_count,1)
            self.assertEqual(download.call_count,1)
        self.assertEqual(Path(first).read_bytes(),b'normalized fixture image')
        self.assertEqual(len(list(Path(config.CACHE_DIR).rglob('*.*'))),1)
        self.assertNotIn('private',first)

    def test_failure_cooldown_and_maintenance_prevent_repeated_browser_starts(self):
        with patch.object(author_profiles,'fetch_profile',return_value=None) as profile:
            self.assertIsNone(author_profiles.get_avatar('weibo','123'))
            self.assertIsNone(author_profiles.get_avatar('weibo','123'))
            self.assertEqual(profile.call_count,1)
            with patch.object(config,'MAINTENANCE_ACTIVE',True):
                self.assertIsNone(author_profiles.get_avatar('weibo','456'))
            self.assertEqual(profile.call_count,1)
        self.assertEqual(author_profiles._pending,set())
