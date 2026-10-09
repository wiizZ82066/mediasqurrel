"""Read-only source benchmark with an isolated database and image cache."""
import argparse
import json
import os
from pathlib import Path
import statistics
import sys
import tempfile
import time


def measure(fn, count=1):
    samples = []
    value = None
    for _ in range(count):
        started = time.perf_counter()
        value = fn()
        samples.append((time.perf_counter() - started) * 1000)
    ordered = sorted(samples)
    return value, {'median_ms': round(statistics.median(samples), 3),
                   'p95_ms': round(ordered[min(len(ordered)-1, int(len(ordered)*.95))], 3), 'samples': count}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    source = Path(args.root).resolve()
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    with tempfile.TemporaryDirectory(prefix='media-squirrel-catalog-') as temporary:
        os.environ['MS_APP_DATA_DIR'] = temporary
        os.environ['MS_LIBRARY_DIR'] = str(source)
        from app import catalog, config, db, library_service, thumbs
        db.migrate()
        root = catalog.register_root(source)
        report = {'method': 'isolated SQLite and application image cache; OS disk cache not flushed',
                  'source_modified': False}
        _, report['initial_metadata_index'] = measure(lambda: catalog.scan_root(root['id']))
        _, report['unchanged_incremental_scan'] = measure(lambda: catalog.scan_root(root['id']), 5)
        page, report['first_page_60'] = measure(lambda: catalog.list_entries(page_size=60), 30)
        report['entry_count'] = page['total']
        _, report['search'] = measure(lambda: catalog.list_entries(q='2026', page_size=60), 30)
        _, report['authors'] = measure(catalog.list_authors, 30)
        _, report['dates'] = measure(catalog.date_groups, 30)
        report['page_json_bytes'] = len(json.dumps(page, ensure_ascii=False).encode())
        if page['items']:
            entry = page['items'][0]
            _, report['entry_detail'] = measure(lambda: catalog.get_entry(entry['id']), 30)
            if entry.get('cover'):
                full = source / entry['rel_dir'] / entry['cover']
                _, report['first_thumb_fresh_cache'] = measure(lambda: thumbs.get_thumb(str(full), width=480))
                _, report['first_thumb_warm_cache'] = measure(lambda: thumbs.get_thumb(str(full), width=480), 30)
                _, report['first_entry_cover_analysis'] = measure(lambda: library_service._analyze(entry['id'], entry['signature']))
        Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
