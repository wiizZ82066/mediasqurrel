"""Publish one offline batch to its paired primary. Never import the old local library."""
import hashlib
import json
from pathlib import Path, PureWindowsPath
import shutil

from . import archive, catalog, config, db
from .runtime import InstanceLock


def _child(root, relative):
    rel = Path(relative)
    win = PureWindowsPath(relative)
    if (not rel.parts or rel.is_absolute() or win.drive or win.root or '..' in rel.parts
            or any(':' in part or part in ('.', '..') for part in win.parts)):
        raise ValueError('暂存清单含越界路径')
    path = root / rel
    if root.resolve() != root or path.resolve() != path or not path.is_relative_to(root) or path == root:
        raise ValueError('暂存清单包含链接或超出媒体目录')
    return path


def _hash(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _files(folder, *, hash_contents=True):
    if archive.entry_status(folder, verify_files=True) != 'complete':
        raise ValueError('暂存作品不完整，未迁回')
    manifest = archive.read_manifest(folder)
    declared = {str(f['path']) for f in manifest['files']} | {'entry.json'}
    actual = set()
    for p in folder.rglob('*'):
        rel = p.relative_to(folder).as_posix()
        _child(folder, rel)
        if not p.is_dir():
            if not p.is_file():
                raise ValueError('作品目录内有无法读取的文件')
            actual.add(rel)
    if actual != declared:
        raise ValueError('作品清单与实际文件不一致，已保留原件')
    return manifest, {rel: _hash(_child(folder, rel)) if hash_contents else None for rel in sorted(actual)}


def _copy(source, destination, digest):
    # Only writes inside an exclusively owned staging directory. Verify an
    # already copied file before reusing it after a loss of power or connection.
    if destination.is_file() and _hash(destination) == digest:
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    with archive.atomic_output(destination) as temporary:
        shutil.copyfile(source, temporary)
        if _hash(Path(temporary)) != digest:
            raise OSError('迁回文件校验失败')


def _media(slot, primary, task, source_rel):
    database, library = Path(primary[0]), Path(primary[1]).resolve()
    source = _child(slot / 'library', source_rel)
    if len(Path(source_rel).parts) != 2:
        raise ValueError('下载目录不是按作者保存的单个作品')
    manifest, hashes = _files(source)
    platform, item_id = manifest.get('platform'), str(manifest.get('item_id') or '')
    subscription = json.loads(task['metadata_json']).get('subscription') or {}
    if (platform not in ('douyin', 'weibo') or not item_id.isalnum() or
            subscription and (platform != subscription.get('platform') or item_id != str(subscription.get('item_id')))):
        raise ValueError('任务与暂存作品的身份不符')
    with db.connect(database) as c:
        hits = c.execute("""SELECT e.rel_dir FROM media_entries e JOIN media_roots r ON r.id=e.root_id
            WHERE r.path=? AND platform=? AND item_id=? AND availability='present'""",
                         (str(library), platform, item_id)).fetchall()
    if len(hits) > 1:
        raise ValueError('首选库已有多个同一作品，保留本机副本')
    dest_rel = hits[0][0] if hits else source_rel
    dest = _child(library, dest_rel)
    if database.parent.is_relative_to(library) and dest.is_relative_to(database.parent):
        raise ValueError('不能向应用数据库目录写入媒体')
    installed = False
    if not dest.exists():
        config.require_database(database)
        dest.parent.mkdir(parents=True, exist_ok=True)
        # Confined to this private batch; interrupted transfers resume in place.
        stage = _child(library, f'.subscription-transfer/{slot.name}/{task["id"]}')
        stage.mkdir(parents=True, exist_ok=True)
        need = sum((_child(source, r)).stat().st_size for r in hashes if not _child(stage, r).exists())
        if shutil.disk_usage(library).free < need + 16 * 1024 * 1024:
            raise OSError('媒体盘可用空间不足，下载仍保存在本机')
        for rel, digest in hashes.items():
            config.require_database(database)
            _copy(_child(source, rel), _child(stage, rel), digest)
        if _files(stage, hash_contents=False)[1].keys() != hashes.keys():
            raise ValueError('暂存迁回目录出现其他文件')
        config.require_database(database)
        if not dest.exists():
            stage.rename(dest)
            installed = True
        for directory in (stage.parent, stage.parent.parent):
            try:
                directory.rmdir()
            except OSError:
                pass
    # A rename publishes the very files whose hashes _copy just verified.
    other, checked = (manifest, hashes) if installed else _files(dest)
    if (other.get('platform') != platform or str(other.get('item_id')) != item_id or
            {k: v for k, v in checked.items() if k != 'entry.json'} !=
            {k: v for k, v in hashes.items() if k != 'entry.json'}):
        raise ValueError('目标已有不同文件，未覆盖；下载仍保存在本机')
    root = catalog.register_root(library, label='默认媒体库', database=database)
    result = catalog.scan_entry(root['id'], dest_rel, database=database)
    entry = catalog.find_entry(root['id'], dest_rel, database=database)
    if result['status'] != 'success' or not entry or entry['item_id'] != item_id or entry['platform'] != platform:
        raise ValueError('迁回后的作品尚未建立索引，仍保留本机文件')
    return dest_rel, entry['id'], root['id'], hashes


def _columns(c, table):
    return [r[1] for r in c.execute(f'PRAGMA main.table_info({table})')]


def _insert(c, table, record):
    keys = _columns(c, table)
    c.execute(f'INSERT INTO {table} ({",".join(keys)}) VALUES ({",".join("?" for _ in keys)})',
              [record[k] for k in keys])


def _task(slot, primary, task):
    database, library = Path(primary[0]), Path(primary[1])
    task_id = task['id']
    if len(task_id) != 32 or any(ch not in '0123456789abcdef' for ch in task_id):
        raise ValueError('暂存任务标识不正确')
    original = json.loads(task['metadata_json'])
    with db.connect(database) as c:
        exists = c.execute('SELECT metadata_json,output_rel FROM tasks WHERE id=?', (task_id,)).fetchone()
    if exists and json.loads(exists['metadata_json']).get('standby_batch') != slot.name:
        raise ValueError('任务标识冲突，未覆盖首选库记录')
    if exists:
        if task['status'] != 'success':
            return
        # Both receipts are written only after files, logs, task and catalog
        # link committed. A blocked sibling shouldn't rehash videos every retry.
        with db.connect(slot / 'app.db') as local:
            receipt = local.execute('SELECT dest_rel FROM standby_files WHERE task_id=?', (task_id,)).fetchone()
        if receipt and receipt[0] == exists['output_rel']:
            folder = _child(library.resolve(), receipt[0])
            if archive.entry_status(folder, verify_files=True) == 'complete':
                return
    source_rel = dest_rel = None
    hashes = {}
    if task['status'] == 'success':
        source = Path(task['output_dir'] or '')
        try:
            source_rel = source.relative_to(slot / 'library').as_posix()
        except ValueError:
            raise ValueError('暂存任务指向工作区外，未移动外部文件') from None
        dest_rel, entry_id, root_id, hashes = _media(slot, primary, task, source_rel)
    elif task['status'] not in ('failed', 'cancelled', 'interrupted', 'skipped'):
        raise ValueError('还有未完成的暂存任务')
    with db.connect(slot / 'app.db') as local:
        archives = local.execute('SELECT * FROM task_log_archives WHERE task_id=?', (task_id,)).fetchall()
    for a in archives:
        src = _child(slot / 'logs', a['path'])
        dest = _child(database.parent / 'logs', a['path'])
        if Path(a['path']).parts[0] != task_id:
            raise ValueError('日志不属于此任务')
        digest = _hash(src)
        config.require_database(database)
        if dest.exists():
            if _hash(dest) != digest:
                raise ValueError('已有同名日志，未覆盖')
        else:
            # Exclusive install; an interrupted copy may be retried from the unchanged local archive.
            dest.parent.mkdir(parents=True, exist_ok=True)
            staging = _child(database.parent / 'logs', f'.standby-{slot.name}/{a["path"]}')
            _copy(src, staging, digest)
            if not dest.exists():
                staging.rename(dest)
    with db.connect(database) as c:
        c.execute('ATTACH DATABASE ? AS local', ((slot / 'app.db').as_uri() + '?mode=ro',))
        c.execute('BEGIN IMMEDIATE')
        if not exists:
            values = dict(task)
            params = json.loads(values['params_json'])
            if 'out' in params:
                params['out'] = str(library)
            values.update(metadata_json=json.dumps({**original, 'standby_batch': slot.name}, ensure_ascii=False),
                          params_json=json.dumps(params, ensure_ascii=False),
                          output_dir=str(library / dest_rel) if dest_rel else None, output_rel=dest_rel)
            _insert(c, 'tasks', values)
        for table in ('task_logs', 'task_log_archives'):
            c.execute(f'INSERT OR IGNORE INTO main.{table} SELECT * FROM local.{table} WHERE task_id=?', (task_id,))
        if dest_rel:
            # We hold the primary database's process lock. A pre-outage task
            # for this exact post may still appear queued/running on that disk.
            # Keep its history, but don't launch it again for an already verified file.
            if subscription := original.get('subscription'):
                reason = '媒体盘离线时此任务中断；同一作品已在本机暂存完成并迁回'
                for prior in c.execute("SELECT id,next_log_seq FROM tasks WHERE content_key=? AND id<>? "
                        "AND status IN ('queued','running') AND json_extract(metadata_json,'$.subscription.platform')=? "
                        "AND json_extract(metadata_json,'$.subscription.item_id')=?",
                        (task['content_key'], task_id, subscription.get('platform'), subscription.get('item_id'))).fetchall():
                    at = db.utc_now()
                    c.execute("INSERT INTO task_logs VALUES(?,?,?,'stderr',?)", (prior['id'], prior['next_log_seq'], at, reason))
                    c.execute("UPDATE tasks SET status='interrupted',finished_at=?,error=?,"
                              "progress_json=json_set(progress_json,'$.label',?),next_log_seq=next_log_seq+1 WHERE id=?",
                              (at, reason, reason, prior['id']))
            c.execute('INSERT OR IGNORE INTO task_media VALUES (?,?,?)', (task_id, entry_id, root_id))
    if source_rel:
        with db.connect(slot / 'app.db') as c:
            c.execute('INSERT OR REPLACE INTO standby_files VALUES(?,?,?,?)',
                      (task_id, source_rel, dest_rel, json.dumps(hashes)))


def _history(slot, database):
    with db.connect(database) as c:
        c.execute('ATTACH DATABASE ? AS local', ((slot / 'app.db').as_uri() + '?mode=ro',))
        c.execute('BEGIN IMMEDIATE')
        owners = """SELECT s.id FROM subscriptions s JOIN local.subscriptions l
            ON l.id=s.id AND l.platform=s.platform AND l.blogger_id=s.blogger_id"""
        for table in ('subscription_scans', 'schedule_runs'):
            c.execute(f"INSERT OR IGNORE INTO main.{table} SELECT * FROM local.{table} WHERE status<>'running' AND sub_id IN ({owners})")
        c.execute(f"""UPDATE subscriptions AS s SET (last_scan_at,last_status,last_error)=
            (SELECT last_scan_at,last_status,last_error FROM local.subscriptions WHERE id=s.id)
            WHERE id IN ({owners}) AND last_scan_at<(SELECT last_scan_at FROM local.subscriptions WHERE id=s.id)""")
        c.execute("""INSERT OR IGNORE INTO main.seen_items SELECT i.* FROM local.seen_items i
                  JOIN local.standby_changes USING(platform,blogger_id,item_id)
                  JOIN main.subscriptions USING(platform,blogger_id)""")
        c.execute("""INSERT OR IGNORE INTO main.subscription_state SELECT l.* FROM local.subscription_state l
                  JOIN main.subscriptions s USING(platform,blogger_id)
                  WHERE EXISTS(SELECT 1 FROM local.subscription_scans x WHERE x.sub_id=s.id
                    AND x.platform=s.platform AND x.blogger_id=s.blogger_id AND x.status='success' AND x.baseline=1)""")
        keys = _columns(c, 'subscription_items')
        updates = ','.join(f'{k}=excluded.{k}' for k in keys if k not in ('platform', 'blogger_id', 'item_id', 'discovered_at'))
        c.execute(f"""INSERT INTO main.subscription_items SELECT i.* FROM local.subscription_items i
            JOIN local.standby_changes USING(platform,blogger_id,item_id)
            WHERE EXISTS(SELECT 1 FROM main.subscriptions s WHERE s.platform=i.platform AND s.blogger_id=i.blogger_id)
            ON CONFLICT(platform,blogger_id,item_id) DO UPDATE SET {updates}
            WHERE main.subscription_items.status NOT IN ('downloaded','cancelled','skipped')
            AND (main.subscription_items.status NOT IN ('queued','running','dispatching')
                 OR NOT EXISTS(SELECT 1 FROM tasks t WHERE t.id=main.subscription_items.task_id AND t.status IN ('queued','running')))""")
        fields = 'next_due,last_due,retry_due,retry_count,last_status,last_error'
        c.execute(f"""UPDATE main.subscription_schedules AS s SET ({fields})=
            (SELECT {fields} FROM local.subscription_schedules WHERE id=s.id) WHERE sub_id IN ({owners})
            AND EXISTS(SELECT 1 FROM local.subscription_schedules l WHERE l.id=s.id AND l.revision=s.revision
                       AND COALESCE(l.last_due,'')>COALESCE(s.last_due,''))""")
        c.execute(f"""INSERT INTO main.subscription_coverage SELECT * FROM local.subscription_coverage
            WHERE sub_id IN ({owners}) ON CONFLICT(sub_id) DO UPDATE SET last_success_at=excluded.last_success_at,
            covered_until=excluded.covered_until,coverage_json=excluded.coverage_json
            WHERE COALESCE(excluded.covered_until,'')>COALESCE(main.subscription_coverage.covered_until,'')""")


def handback(slot, primary):
    database = Path(primary[0])
    config.require_database(database)
    if (not slot or slot != config.standby_slot() or not (slot / 'used').is_file()
            or (slot / 'used').read_text(encoding='ascii') != slot.name):
        raise ValueError('暂存批次不属于当前媒体盘')
    lock = InstanceLock(database.parent, filename='.database-owner.lock').acquire()
    try:
        with db.connect(slot / 'app.db') as c, db.connect(database) as remote:
            if c.execute('PRAGMA user_version').fetchone()[0] != remote.execute('PRAGMA user_version').fetchone()[0]:
                raise ValueError('两个数据库版本不同，暂未迁回')
        after, count, conflicts, first = '', 0, 0, ''
        while True:
            with db.connect(slot / 'app.db') as c:
                tasks = c.execute('SELECT * FROM tasks WHERE id>? ORDER BY id LIMIT 50', (after,)).fetchall()
            if not tasks:
                break
            for task in tasks:
                try:
                    _task(slot, primary, task)
                except ValueError as error:
                    conflicts += 1
                    if not first:
                        first = f"任务 {task['id'][:8]}：{error}"
                count += 1
            after = tasks[-1]['id']
        if conflicts:
            raise ValueError(f'{conflicts} 条暂存记录需处理。{first}')
        _history(slot, database)
        config.require_database(database)
        archive.atomic_write_text(slot / 'handback', slot.name)
        return count
    finally:
        lock.release()


def release_media(slot, primary):
    """The primary service is open again. Unlink only receipted, still identical local files."""
    database, library = Path(primary[0]), Path(primary[1])
    if not (slot / 'handback').is_file() or (slot / 'handback').read_text(encoding='ascii') != slot.name:
        return
    with db.connect(slot / 'app.db') as local:
        rows = local.execute('SELECT * FROM standby_files').fetchall()
        logs = local.execute('SELECT * FROM task_log_archives').fetchall()
    for row in rows:
        with db.connect(database) as c:
            receipt = c.execute("SELECT 1 FROM tasks t JOIN task_media m ON m.task_id=t.id "
                                "WHERE t.id=? AND t.output_rel=? AND json_extract(t.metadata_json,'$.standby_batch')=?",
                                (row['task_id'], row['dest_rel'], slot.name)).fetchone()
        if not receipt:
            continue
        source = _child(slot / 'library', row['source_rel'])
        dest = _child(library.resolve(), row['dest_rel'])
        if not source.is_dir() or not dest.is_dir():
            continue
        original = archive.read_manifest(source)
        manifest, seen = _files(dest)
        checks = json.loads(row['checks'])
        existing = {p.relative_to(source).as_posix() for p in source.rglob('*') if not p.is_dir()}
        if (not original or not existing.issubset(checks)
                or any(_hash(_child(source, k)) != checks[k] for k in existing)
                or (original.get('platform'), original.get('item_id')) !=
                (manifest.get('platform'), manifest.get('item_id')) or
                any(seen.get(k) != v for k, v in checks.items() if k != 'entry.json')):
            continue
        # Keep the marker until last, so interruption leaves a resumable receipt.
        for rel in sorted(checks, key=lambda r: r == 'entry.json'):
            file = _child(source, rel)
            if file.is_file() and _hash(file) == checks[rel]:
                config.require_database(database)
                file.unlink()
        for folder in (source, source.parent):
            try:
                folder.rmdir()
            except OSError:
                pass
    for log in logs:
        with db.connect(database) as c:
            receipt = c.execute('SELECT 1 FROM task_log_archives WHERE task_id=? AND path=?', (log['task_id'], log['path'])).fetchone()
        src = _child(slot / 'logs', log['path'])
        dst = _child(database.parent / 'logs', log['path'])
        if receipt and src.is_file() and dst.is_file() and _hash(src) == _hash(dst):
            config.require_database(database)
            src.unlink()


def retire_if_empty(slot):
    """Drop a retired journal only after publishing its replacement; leave any unexpected file alone."""
    if (slot.parent != config.STANDBY_BASE or slot.resolve() != slot or slot == config.standby_slot()
            or not (slot / 'handback').is_file()
            or (slot / 'handback').read_text(encoding='ascii') != slot.name):
        return
    owned = {'app.db', 'app.db-wal', 'app.db-shm', '.database-owner.lock', 'settings.json', 'used', 'handback'}
    paths = sorted(slot.rglob('*'), key=lambda p: len(p.parts), reverse=True)
    if any(p.resolve() != p or not p.is_dir() and (p.parent != slot or p.name not in owned) for p in paths):
        return
    for path in paths:
        if path.is_dir():
            path.rmdir()
        else:
            path.unlink(missing_ok=True)
    slot.rmdir()
