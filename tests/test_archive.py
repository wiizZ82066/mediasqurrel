"""Controlled archive tests; every output stays in an independent temp directory."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app import archive
import douyin_downloader as douyin
import weibo_downloader as weibo


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="mediasquirrel-archive-test-")
        self.root = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)

    def begin(self, item_id="Ab123", author="example", date="26-10-09"):
        return archive.begin_entry(str(self.root), author, date, "weibo", item_id,
                                   "https://weibo.com/123/Ab123?tracking=discard")

    def finish(self, entry):
        archive.atomic_write_text(Path(entry.folder) / "context.md", "# Example\n")
        entry.complete(["context.md"])

    def test_identity_isolated_same_date_and_legacy_untouched(self):
        legacy = self.root / "example" / "26-10-09"
        legacy.mkdir(parents=True)
        (legacy / "context.md").write_text("original", encoding="utf-8")
        first, second = self.begin(), self.begin("Cd456")
        self.assertNotEqual(first.folder, second.folder)
        self.finish(first)
        self.finish(second)
        self.assertEqual((legacy / "context.md").read_text(encoding="utf-8"), "original")
        self.assertEqual(archive.entry_status(legacy), "legacy")

    def test_partial_retry_reuses_id_across_date_and_nickname_change(self):
        original = self.begin()
        self.assertEqual(archive.entry_status(original.folder), "partial")
        retried = self.begin(author="renamed", date="26-10-10")
        self.assertEqual(retried.folder, original.folder)
        self.assertEqual(retried.marker["entry_id"], original.marker["entry_id"])
        self.assertEqual(retried.marker["attempt"], 2)
        self.finish(retried)
        self.assertTrue(self.begin().existing_complete)
        self.assertNotIn("?", archive.read_manifest(retried.folder)["source_url"])

    def test_complete_requires_existing_nonempty_outputs_and_rechecks_on_retry(self):
        entry = self.begin()
        with self.assertRaises(ValueError):
            entry.complete(["context.md", "missing.jpg"])
        self.assertEqual(archive.entry_status(entry.folder), "partial")
        self.finish(entry)
        (Path(entry.folder) / "context.md").unlink()
        self.assertEqual(archive.entry_status(entry.folder, verify_files=True), "partial")
        retried = self.begin()
        self.assertFalse(retried.existing_complete)
        self.assertEqual(archive.entry_status(retried.folder), "partial")

    def test_atomic_marker_failure_never_publishes_success(self):
        entry = self.begin()
        archive.atomic_write_text(Path(entry.folder) / "context.md", "# Example")
        with patch.object(archive.os, "replace", side_effect=OSError("simulated full disk")):
            with self.assertRaises(OSError):
                entry.complete(["context.md"])
        self.assertEqual(archive.entry_status(entry.folder), "partial")
        self.assertEqual(list(Path(entry.folder).glob("*.part")), [])

    def test_atomic_media_write_preserves_previous_file_on_interruption(self):
        path = self.root / "existing.jpg"
        path.write_bytes(b"previous")
        with self.assertRaises(RuntimeError):
            with archive.atomic_output(path) as temporary:
                Path(temporary).write_bytes(b"incomplete")
                raise RuntimeError("connection lost")
        self.assertEqual(path.read_bytes(), b"previous")

    def test_malformed_marker_and_unknown_version_never_count_complete(self):
        entry = self.begin()
        self.finish(entry)
        marker_path = Path(entry.folder) / "entry.json"
        marker_path.write_text("{broken", encoding="utf-8")
        self.assertEqual(archive.entry_status(entry.folder), "partial")
        with self.assertRaises(FileExistsError):
            self.begin()
        marker = entry.marker | {"schema_version": 999}
        marker_path.write_text(json.dumps(marker), encoding="utf-8")
        self.assertEqual(archive.entry_status(entry.folder), "partial")
        with self.assertRaises(ValueError):
            self.begin()

    def test_untrusted_identity_and_manifest_paths_cannot_escape_entry(self):
        with self.assertRaises(ValueError):
            self.begin(item_id="../../unsafe")
        entry = self.begin(author="../name")
        self.assertTrue(Path(entry.folder).is_relative_to(self.root))
        (self.root / "outside.jpg").write_bytes(b"outside")
        archive.atomic_write_text(Path(entry.folder) / "context.md", "# Example")
        with self.assertRaises(ValueError):
            entry.complete(["context.md", "../../outside.jpg"])
        self.assertEqual(archive.entry_status(entry.folder), "partial")


class DownloaderArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="mediasquirrel-downloader-test-")
        self.root = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)
        self.output = contextlib.redirect_stdout(io.StringIO())
        self.output.__enter__()
        self.addCleanup(self.output.__exit__, None, None, None)

    def weibo_data(self, item_id="Ab123", photos=None, lives=None):
        return {"username": "example", "publish_time": "26-10-09", "text": "test body",
                "url": f"https://weibo.com/123/{item_id}", "mblogid": item_id,
                "normal_imgs": photos or [], "live_videos": lives or []}

    def douyin_data(self, item_id="12345", cover=False):
        return {"author": {"nickname": "example", "uid": "42", "sec_uid": "example-id"},
                "aweme_id": item_id, "create_time": 1791518400, "desc": "test body",
                "video": {"play_addr": {"url_list": ["https://media.invalid/video"]},
                          "cover": {"url_list": ["https://media.invalid/cover"] if cover else []}}}

    @staticmethod
    def fake_weibo_download(url, path, **_kwargs):
        Path(path).write_bytes(url.encode())
        return True

    @staticmethod
    def fake_douyin_download(url, path, **_kwargs):
        Path(path).write_bytes(url.encode())
        return len(url.encode())

    def test_weibo_same_day_posts_text_only_and_completed_retry(self):
        with patch.object(weibo, "download_file", side_effect=self.fake_weibo_download) as download:
            first = weibo.save_content(self.weibo_data(photos=["one"]), str(self.root))
            second = weibo.save_content(self.weibo_data("Cd456", photos=["two"]), str(self.root))
            repeated = weibo.save_content(self.weibo_data(photos=["one"]), str(self.root))
        self.assertNotEqual(first, second)
        self.assertEqual(first, repeated)
        self.assertEqual(download.call_count, 2)
        self.assertEqual((Path(first) / "photo/img01.jpg").read_bytes(), b"one")
        self.assertEqual((Path(second) / "photo/img01.jpg").read_bytes(), b"two")
        text = weibo.save_content(self.weibo_data("Text123"), str(self.root))
        self.assertEqual(archive.entry_status(text, verify_files=True), "complete")

    def test_weibo_failed_media_remains_partial_and_retry_completes_live_pair(self):
        data = self.weibo_data(photos=["photo"], lives=["movie"])
        with patch.object(weibo, "download_file", return_value=False):
            with self.assertRaises(RuntimeError):
                weibo.save_content(data, str(self.root))
        folder = next((self.root / "example").iterdir())
        self.assertTrue((folder / "context.md").exists())
        self.assertEqual(archive.entry_status(folder), "partial")
        def cover(_movie, poster):
            Path(poster).write_bytes(b"poster")
            return True
        with patch.object(weibo, "download_file", side_effect=self.fake_weibo_download), \
                patch.object(weibo, "extract_cover_from_mov", side_effect=cover):
            result = weibo.save_content(data, str(self.root))
        self.assertEqual(Path(result), folder)
        self.assertEqual(archive.entry_status(folder, verify_files=True), "complete")
        self.assertEqual({x["path"] for x in archive.read_manifest(folder)["files"]},
                         {"context.md", "photo/img01.jpg", "live/live01.mov", "live/live01.jpg"})

    def test_live_cover_failure_cannot_publish_complete(self):
        with patch.object(weibo, "download_file", side_effect=self.fake_weibo_download), \
                patch.object(weibo, "extract_cover_from_mov", return_value=False):
            with self.assertRaises(RuntimeError):
                weibo.save_content(self.weibo_data(lives=["movie"]), str(self.root))
        folder = next((self.root / "example").iterdir())
        self.assertEqual(archive.entry_status(folder), "partial")

    def test_douyin_same_minute_posts_do_not_overwrite_context(self):
        with patch.object(douyin, "download", side_effect=self.fake_douyin_download):
            first = douyin.save_content(self.douyin_data(), str(self.root))
            second = douyin.save_content(self.douyin_data("56789"), str(self.root))
        self.assertNotEqual(first, second)
        self.assertIn("12345.mp4", (Path(first) / "context.md").read_text(encoding="utf-8"))
        self.assertIn("56789.mp4", (Path(second) / "context.md").read_text(encoding="utf-8"))
        self.assertEqual(archive.entry_status(first, verify_files=True), "complete")

    def test_douyin_failed_cover_retains_partial_then_reuses_directory(self):
        def fail_cover(url, path, **kwargs):
            if url.endswith("cover"):
                raise OSError("simulated offline")
            return self.fake_douyin_download(url, path, **kwargs)
        data = self.douyin_data(cover=True)
        with patch.object(douyin, "download", side_effect=fail_cover):
            with self.assertRaises(RuntimeError):
                douyin.save_content(data, str(self.root))
        folder = next((self.root / "example").iterdir())
        self.assertEqual(archive.entry_status(folder), "partial")
        with patch.object(douyin, "download", side_effect=self.fake_douyin_download):
            result = douyin.save_content(data, str(self.root))
        self.assertEqual(Path(result), folder)
        self.assertEqual(archive.entry_status(folder, verify_files=True), "complete")
        self.assertTrue((folder / "12345_cover.jpg").is_file())

    def test_cli_default_is_configured_library_and_explicit_path_is_preserved(self):
        with patch.object(douyin.config, "LIBRARY_ROOT", str(self.root / "configured")):
            self.assertEqual(douyin.parse_args(["sharing text"])[1], str(self.root / "configured"))
            self.assertEqual(douyin.parse_args(["url", "--out", str(self.root)])[1], str(self.root))


if __name__ == "__main__":
    unittest.main()
