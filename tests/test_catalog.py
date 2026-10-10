"""Isolated metadata/index tests. Fixtures are text bytes, never real media."""
import json
import contextlib
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
import uuid

from app import archive, catalog


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="mediasquirrel-catalog-test-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / "source"
        self.root.mkdir()
        self.database = self.base / "isolated.db"
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            connection.executescript(catalog.SCHEMA)
            connection.commit()
        self.root_record = catalog.register_root(str(self.root), database=self.database)
        self.root_id = self.root_record["id"]

    def fixture(self, name="26-10-09", author="Example", item="Ab123", text="example text"):
        folder = self.root / author / name
        (folder / "photo").mkdir(parents=True)
        (folder / "photo" / "img01.jpg").write_bytes(b"intentionally-not-decodable-jpeg")
        (folder / "context.md").write_text(
            f"- **原文链接**: https://weibo.com/42/{item}\n- **发布时间**: 26-10-09 14:30\n"
            f"## 页面文本\n```\n{text}\n```\n", encoding="utf-8")
        return folder

    def scan(self, **kwargs):
        return catalog.scan_root(self.root_id, database=self.database, **kwargs)

    def entries(self, **kwargs):
        return catalog.list_entries(root_id=self.root_id, database=self.database, **kwargs)

    def test_metadata_scan_legacy_gallery_and_lazy_summary(self):
        folder = self.fixture()
        (folder / "live").mkdir()
        (folder / "live" / "live01.mov").write_bytes(b"movie")
        (folder / "live" / "live01.jpg").write_bytes(b"poster")
        (folder / "123.mp4").write_bytes(b"video")
        (folder / "123_cover.jpg").write_bytes(b"cover")
        result = self.scan()
        self.assertEqual(result["status"], "success")
        item = self.entries()["items"][0]
        self.assertNotIn("gallery", item)
        self.assertNotIn("search_text", item)
        self.assertEqual(item["counts"], {"photos": 1, "lives": 1, "videos": 1})
        self.assertEqual(item["archive_status"], "legacy")
        detail = catalog.get_entry(item["id"], database=self.database)
        self.assertEqual([g["type"] for g in detail["gallery"]], ["image", "video", "video"])
        self.assertTrue(detail["gallery"][1]["live"])
        self.assertEqual(detail["live_map"], {"live/live01.jpg": "live/live01.mov"})
        self.assertEqual(detail["gallery"][2]["poster"], "123_cover.jpg")
        self.assertEqual(len(detail["cover_candidates"]), 3)

    def test_download_subdirectories_keep_unique_relative_paths_and_exact_location(self):
        first = self.fixture(author='collection/Example', item='PostA')
        second = self.fixture(author='other/Example', item='PostB')
        self.fixture(author='data/Private', item='Excluded')
        self.assertEqual(self.scan()['status'], 'success')
        items = self.entries()['items']
        self.assertEqual(len(items), 2)
        self.assertEqual({item['author'] for item in items}, {'Example'})
        self.assertEqual({item['rel_dir'] for item in items}, {'collection/Example/26-10-09', 'other/Example/26-10-09'})
        a = catalog.locate_entry(first / 'photo' / 'img01.jpg', database=self.database)
        b = catalog.locate_entry(second, database=self.database)
        self.assertNotEqual(a['id'], b['id'])
        self.assertEqual(a['item_id'], 'PostA')

    def test_cover_live_path_tracks_exact_poster_and_clears_for_regular_cover(self):
        folder = self.fixture()
        (folder / "live").mkdir()
        for name in ("live01", "live02"):
            (folder / "live" / (name + ".mov")).write_bytes(b"movie")
            (folder / "live" / (name + ".jpg")).write_bytes(b"poster")
        (folder / "video.mp4").write_bytes(b"video")
        (folder / "video_cover.jpg").write_bytes(b"video poster")
        self.scan()
        item = self.entries()["items"][0]
        for cover, kind, expected in (("live/live01.jpg", "live", "live/live01.mov"),
                                      ("live/live02.jpg", "live", "live/live02.mov"),
                                      ("photo/img01.jpg", "image", None),
                                      ("video_cover.jpg", "video", None), (None, None, None)):
            catalog.update_cover(item["id"], cover, kind, database=self.database)
            summary = self.entries()["items"][0]
            detail = catalog.get_entry(item["id"], database=self.database)
            self.assertEqual(summary["cover_live_rel"], expected)
            self.assertEqual(detail["cover_live_rel"], expected)
            self.assertNotIn("gallery", summary)
            self.assertNotIn("assets", summary)

    def test_live_cover_page_uses_one_bounded_batch_query_not_per_entry_details(self):
        for number in range(4):
            folder = self.fixture(f"26-10-0{number + 1}", item=f"Item{number}")
            (folder / "live").mkdir()
            (folder / "live" / "live01.mov").write_bytes(b"movie")
            (folder / "live" / "live01.jpg").write_bytes(b"poster")
        self.scan()
        for item in self.entries()["items"]:
            catalog.update_cover(item["id"], "live/live01.jpg", "live", database=self.database)
        queries = []
        connect = catalog.db.connect
        @contextlib.contextmanager
        def traced(database=None):
            with connect(database) as connection:
                connection.set_trace_callback(queries.append)
                yield connection
        with patch.object(catalog.db, "connect", traced), patch.object(catalog, "get_entry", side_effect=AssertionError("details are not list data")):
            for size in (1, 3):
                queries.clear()
                page = self.entries(page_size=size)
                reads = [query for query in queries if query.lstrip().upper().startswith(("SELECT", "WITH"))]
                self.assertEqual(len(reads), 3)  # count, bounded page, bounded Live pairing
                self.assertEqual(len(page["items"]), size)
                self.assertTrue(all(item["cover_live_rel"] == "live/live01.mov" for item in page["items"]))
                self.assertEqual(sum("WITH cover_page" in query for query in reads), 1)

    def test_cover_analysis_event_pairing_matches_persisted_cover_and_discards_stale_work(self):
        import numpy as np
        from app import config, library_service, media_library, thumbs
        folder = self.fixture()
        (folder / "live").mkdir()
        (folder / "live" / "live01.mov").write_bytes(b"movie")
        (folder / "live" / "live01.jpg").write_bytes(b"poster")
        self.scan()
        item = self.entries()["items"][0]
        detail = catalog.get_entry(item["id"], database=self.database)
        with patch.object(config, "DB_PATH", str(self.database)), patch.object(config, "MAINTENANCE_ACTIVE", False), \
                patch.object(library_service, "_stopping", False), patch.object(catalog, "get_entry", return_value=detail), \
                patch.object(thumbs, "get_thumb", return_value="fixture-thumb"), \
                patch.object(thumbs, "_imread_unicode", return_value=np.zeros((10, 10, 3), dtype=np.uint8)), \
                patch.object(media_library, "_get_face_detector", return_value=None):
            for cover, kind, expected in (("live/live01.jpg", "live", "live/live01.mov"), ("photo/img01.jpg", "image", None)):
                detail["cover_candidates"] = [{"rel": cover, "kind": kind}]
                event = library_service._analyze(item["id"], item["signature"])
                self.assertEqual(event["cover_live_rel"], expected)
                self.assertEqual(self.entries()["items"][0]["cover_live_rel"], expected)
            with patch.object(catalog, "update_cover", return_value=False):
                self.assertIsNone(library_service._analyze(item["id"], item["signature"]))

    def test_marked_partial_and_complete_text_only_preserve_identity(self):
        partial = archive.begin_entry(str(self.root), "Example", "26-10-09", "weibo", "Ab123")
        (Path(partial.folder) / "context.md").write_text("# text only", encoding="utf-8")
        self.scan()
        item = self.entries(status="partial")["items"][0]
        self.assertEqual(item["id"], partial.marker["entry_id"])
        self.assertEqual(self.entries(status="complete")["total"], 0)
        partial.complete(["context.md"])
        self.scan()
        completed = self.entries(status="complete")["items"][0]
        self.assertEqual(completed["id"], item["id"])
        self.assertEqual(completed["media_count"], 0)
        self.assertEqual(catalog.get_entry(item["id"], database=self.database)["gallery"], [])

    def test_unchanged_scan_skips_context_parsing_and_preserves_cover(self):
        self.fixture()
        self.scan()
        item = self.entries()["items"][0]
        catalog.update_cover(item["id"], "photo/img01.jpg", "image", {"x": .4, "y": .3},
                             signature=item["signature"], database=self.database)
        with patch.object(catalog, "_metadata", side_effect=AssertionError("unchanged metadata was reparsed")):
            result = self.scan()
        self.assertEqual(result["changed"], 0)
        self.assertEqual(self.entries()["items"][0]["cover_face"], {"x": .4, "y": .3})

    def test_changed_file_retains_entry_id_and_rejects_stale_cover_work(self):
        folder = self.fixture()
        self.scan()
        before = self.entries()["items"][0]
        image = folder / "photo" / "img01.jpg"
        image.write_bytes(b"new source media")
        stat = image.stat()
        os.utime(image, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1000000000))
        result = self.scan()
        after = self.entries()["items"][0]
        self.assertEqual(result["changed"], 1)
        self.assertEqual(before["id"], after["id"])
        self.assertNotEqual(before["signature"], after["signature"])
        self.assertFalse(catalog.update_cover(after["id"], "photo/img01.jpg", "image",
                                             signature=before["signature"], database=self.database))

    def test_cancelled_or_failed_scan_never_marks_unvisited_entries_missing(self):
        folder = self.fixture()
        self.scan()
        cancel = threading.Event()
        cancel.set()
        self.assertEqual(self.scan(cancel=cancel)["status"], "cancelled")
        with patch.object(catalog, "_inventory", side_effect=PermissionError("simulated unavailable file")):
            self.assertEqual(self.scan()["status"], "failed")
        self.assertEqual(self.entries()["total"], 1)
        (folder / "photo" / "img01.jpg").unlink()
        (folder / "photo").rmdir()
        (folder / "context.md").unlink()
        folder.rmdir()
        self.assertEqual(self.scan()["status"], "success")
        self.assertEqual(self.entries()["total"], 0)
        self.assertEqual(self.entries(status="missing")["total"], 1)

    def test_cancel_checkpoint_can_resume_without_rewriting_completed_batch(self):
        self.fixture("26-10-08", item="Ab1")
        self.fixture("26-10-09", item="Ab2")
        event = threading.Event()
        result = self.scan(batch_size=1, cancel=event, progress=lambda _state: event.set())
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["processed"], 1)
        resumed = self.scan(batch_size=1)
        self.assertEqual(resumed["status"], "success")
        self.assertEqual(resumed["processed"], 2)
        self.assertEqual(resumed["changed"], 1)

    def test_preflight_registration_and_exact_task_location_do_not_write_media(self):
        folder = self.fixture()
        before = {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        result = catalog.preflight_root(self.root)
        self.assertEqual((result["entries"], result["files"]), (1, 2))
        self.assertEqual(catalog.register_root(str(self.root), database=self.database)["id"], self.root_id)
        self.scan()
        expected = self.entries()["items"][0]["id"]
        self.assertEqual(catalog.locate_entry(folder / "photo" / "img01.jpg", database=self.database)["id"], expected)
        self.assertEqual(catalog.find_entry(self.root_id, "Example/26-10-09", database=self.database)["id"], expected)
        self.assertIsNone(catalog.locate_entry(self.base / "other", database=self.database))
        after = {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_search_date_and_platform_filters_use_full_context_and_local_day(self):
        self.fixture(text="a" * 150 + " unique needle")
        self.scan()
        self.assertEqual(self.entries(q="unique needle")["total"], 1)
        self.assertEqual(self.entries(date_from="2026-10-09", date_to="2026-10-09")["total"], 1)
        self.assertEqual(self.entries(date_to="2026-10-08")["total"], 0)
        self.assertEqual(self.entries(platform="weibo", media_type="image")["total"], 1)
        self.assertEqual(self.entries(platform="douyin")["total"], 0)
        groups = catalog.date_groups(root_id=self.root_id, database=self.database)
        self.assertEqual(groups, [{"date": "2026-10", "count": 1}])
        self.assertEqual(catalog.list_authors(root_id=self.root_id, database=self.database)["items"][0]["name"], "Example")

    def test_rebind_failure_and_cancel_leave_old_root_then_verified_copy_keeps_ids(self):
        self.fixture()
        self.scan()
        original_id = self.entries()["items"][0]["id"]
        copied = self.base / "copy"
        shutil.copytree(self.root, copied)
        image = copied / "Example" / "26-10-09" / "photo" / "img01.jpg"
        original_bytes = image.read_bytes()
        image.write_bytes(b"x" * len(original_bytes))
        with self.assertRaises(ValueError):
            catalog.rebind_root(self.root_id, copied, database=self.database)
        self.assertEqual(catalog.get_root(self.root_id, database=self.database)["path"], str(self.root.resolve()))
        image.write_bytes(original_bytes)
        with self.assertRaises(InterruptedError):
            catalog.rebind_root(self.root_id, copied, cancel=lambda: True, database=self.database)
        result = catalog.rebind_root(self.root_id, copied, database=self.database)
        self.assertEqual(result["id"], self.root_id)
        self.assertEqual(result["path"], str(copied.resolve()))
        self.assertEqual(self.scan()["status"], "success")
        self.assertEqual(self.entries()["items"][0]["id"], original_id)
        self.assertTrue(self.root.is_dir())

    def test_duplicate_platform_identity_is_reported_without_merging(self):
        first = archive.begin_entry(str(self.root), "Example", "26-10-09", "weibo", "Ab123")
        (Path(first.folder) / "context.md").write_text("# example", encoding="utf-8")
        first.complete(["context.md"])
        copied = self.root / "Example" / "26-10-10_weibo_Ab123"
        shutil.copytree(first.folder, copied)
        self.scan()
        items = self.entries()["items"]
        self.assertEqual(len(items), 2)
        self.assertEqual(len({item["id"] for item in items}), 2)
        self.assertTrue(all(item["conflict"] == "duplicate_platform_id" for item in items))

    def test_moved_archive_reuses_stable_entry_and_asset_ids(self):
        original = archive.begin_entry(str(self.root), "Example", "26-10-09", "weibo", "Ab123")
        (Path(original.folder) / "context.md").write_text("# example", encoding="utf-8")
        (Path(original.folder) / "photo").mkdir()
        (Path(original.folder) / "photo" / "img01.jpg").write_bytes(b"test")
        original.complete(["context.md", "photo/img01.jpg"])
        self.scan()
        identity = self.entries()["items"][0]["id"]
        asset_id = catalog.get_entry(identity, database=self.database)["assets"][0]["id"]
        renamed_author = self.root / "Renamed"
        renamed_author.mkdir()
        Path(original.folder).rename(renamed_author / Path(original.folder).name)
        self.assertEqual(self.scan()["status"], "success")
        item = self.entries()["items"][0]
        self.assertEqual(item["id"], identity)
        self.assertEqual(item["author"], "Renamed")
        self.assertEqual(catalog.get_entry(identity, database=self.database)["assets"][0]["id"], asset_id)
        self.assertEqual(self.entries(status="missing")["total"], 0)

    def test_twenty_thousand_entries_return_only_a_bounded_page(self):
        self.fixture()
        self.scan()
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            connection.row_factory = sqlite3.Row
            template = dict(connection.execute("SELECT * FROM media_entries").fetchone())
            rows = []
            for number in range(19999):
                item = template | {"id": str(uuid.uuid4()), "rel_dir": f"Synthetic/{number}",
                                   "author": "Synthetic", "item_id": f"Synthetic{number}"}
                rows.append(tuple(item.values()))
            connection.executemany(f"INSERT INTO media_entries({','.join(template)}) VALUES({','.join('?' for _ in template)})", rows)
            connection.commit()
        first = self.entries(page=1, page_size=60)
        second = self.entries(page=2, page_size=60)
        self.assertEqual(first["total"], 20000)
        self.assertEqual(len(first["items"]), 60)
        self.assertFalse({item["id"] for item in first["items"]} & {item["id"] for item in second["items"]})
        self.assertLess(len(json.dumps(first).encode()), 100000)
        self.assertEqual(len(self.entries(page_size=1000000)["items"]), 200)


if __name__ == "__main__":
    unittest.main()
