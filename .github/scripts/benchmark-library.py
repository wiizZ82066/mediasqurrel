"""Read original media; place every generated cache in an isolated directory.

This measures a fresh application cache, not a flushed operating-system cache.
Use --output only for a local, ignored results file. No filenames are reported.
"""
import argparse
import json
import os
from pathlib import Path
import statistics
import sys
import tempfile
import time


def timed(function, repetitions=1):
    samples = []
    value = None
    for _ in range(repetitions):
        started = time.perf_counter()
        value = function()
        samples.append((time.perf_counter() - started) * 1000)
    return value, {"median_ms": round(statistics.median(samples), 3),
                   "max_ms": round(max(samples), 3), "samples": repetitions}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--output")
    parser.add_argument("--metadata-only", action="store_true")
    args = parser.parse_args()
    source = Path(args.root).resolve()
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    with tempfile.TemporaryDirectory(prefix="media-squirrel-benchmark-") as temporary:
        os.environ["MS_DATA_DIR"] = temporary
        os.environ["MS_LIBRARY_DIR"] = str(source)
        os.environ["MS_APP_DATA_DIR"] = os.path.join(temporary, "data")
        from app import config, media_library as library
        # Deliberately do not call ensure_runtime_dirs: the source is read-only.
        result = {"method": "fresh application cache; OS disk cache not flushed",
                  "source_modified": False, "metadata_only": args.metadata_only}
        original = library._pick_best_cover
        library._pick_best_cover = lambda _base, candidates: (candidates[0][0], None, candidates[0][1])
        entries, result["metadata_scan"] = timed(library._scan_root_nocache, 5)
        library._pick_best_cover = original
        flat = [entry for author in entries for entry in author["entries"]]
        files = sum(len(entry["photos"]) + len(entry["lives"]) + len(entry["videos"]) for entry in flat)
        result["inventory"] = {"authors": len(entries), "legacy_entry_folders": len(flat),
                               "viewable_items": files, "media_bytes": sum(entry["size"] for entry in flat)}
        physical_count, physical_bytes = 0, 0
        for entry in flat:
            for folder, directories, names in os.walk(source / entry["rel_dir"], followlinks=False):
                directories[:] = [name for name in directories if not os.path.islink(os.path.join(folder, name))]
                for name in names:
                    path = Path(folder) / name
                    if not path.is_symlink() and path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".mp4", ".mov", ".m4v", ".webm"}:
                        physical_count += 1
                        physical_bytes += path.stat().st_size
        result["inventory"].update(media_files=physical_count, media_file_bytes=physical_bytes)
        _value, result["full_json_serialization"] = timed(lambda: json.dumps(entries, ensure_ascii=False), 20)
        if not args.metadata_only:
            faces = {"calls": 0, "total_ms": 0.0}
            original_face = library._detect_face

            def face(*values):
                started = time.perf_counter()
                try:
                    return original_face(*values)
                finally:
                    faces["calls"] += 1
                    faces["total_ms"] += (time.perf_counter() - started) * 1000

            library._detect_face = face
            _value, result["fresh_cover_and_thumbnail_scan"] = timed(lambda: library.scan_root(refresh=True))
            _value, result["memory_cache_read"] = timed(library.scan_root, 30)
            library._lib_cache_mem = None
            _value, result["json_cache_read"] = timed(library.scan_root)
            _value, result["warm_cover_rescan"] = timed(lambda: library.scan_root(refresh=True))
            faces["total_ms"] = round(faces["total_ms"], 3)
            result["face_detection_combined"] = faces
            result["isolated_cache_bytes"] = sum(path.stat().st_size for path in Path(config.CACHE_DIR).rglob("*") if path.is_file())
        payload = json.dumps(result, ensure_ascii=False, indent=2)
        if args.output:
            target = Path(args.output)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(payload + "\n", encoding="utf-8")
        print(payload)


if __name__ == "__main__":
    main()
