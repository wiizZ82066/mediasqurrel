"""Preview exactly one catalog card, then recycle the same, unchanged archives."""
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import stat

from . import catalog, config, db, recycle_bin


def _members(connection, entry_id):
    fields = """SELECT e.*,r.path AS root_path FROM media_entries e
        JOIN media_roots r ON r.id=e.root_id WHERE e.availability='present' AND """
    anchor = connection.execute(fields + 'e.id=?', (entry_id,)).fetchone()
    if not anchor:
        raise ValueError('内容已移动或删除，请刷新媒体库')
    # Only archives the viewer actually groups with this card. An independent
    # copy with the same post ID remains separately addressable.
    linked = connection.execute(fields + f"""e.id<>? AND (
        (e.live_companion_of=? AND EXISTS ({catalog._LIVE_ANCHOR_SQL.format(source='e')}))
        OR EXISTS (SELECT 1 FROM live_recordings g JOIN live_recordings p ON p.merged_into=g.id
                   WHERE g.is_collection=1 AND g.entry_id=? AND p.is_aggregate=0 AND p.entry_id=e.id))
        ORDER BY e.root_id,e.rel_dir,e.id""", (entry_id, entry_id, entry_id))
    return [dict(row) for row in linked] + [dict(anchor)]


def _plain_node(path):
    info = path.lstat()
    if (stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
            or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode))):
        raise ValueError('目录中含有文件链接、联接点或特殊文件，请在资源管理器中核对')
    return info


def _folder(entry):
    root = Path(entry['root_path'])
    folder = catalog.entry_folder(root, entry['rel_dir'])
    path = root
    _plain_node(path)
    for part in Path(entry['rel_dir']).parts:
        path /= part
        _plain_node(path)
    if path.resolve() != folder or folder == root.resolve() or not folder.is_relative_to(root.resolve()):
        raise ValueError('内容目录已改变，请更新媒体索引')
    return folder


def _check_idle(connection, entry, folder):
    if config.MAINTENANCE_ACTIVE:
        raise ValueError('数据维护中，请稍后再试')
    key = f"{entry['platform']}:{entry['item_id']}" if entry['item_id'] else None
    active = connection.execute("""SELECT 1 FROM tasks t LEFT JOIN task_media m ON m.task_id=t.id
        WHERE t.status IN ('queued','running') AND (t.content_key=? OR m.entry_id=? OR
        t.output_dir=? COLLATE NOCASE OR t.output_rel=? OR (t.script_id=? AND ? IS NOT NULL AND
        instr('/'||trim(json_extract(t.params_json,'$.url'),'/')||'/', '/'||?||'/')>0)) LIMIT 1""",
        (key, entry['id'], str(folder), entry['rel_dir'], entry['platform'], key, entry['item_id'])).fetchone()
    recording = connection.execute("""SELECT 1 FROM live_recordings WHERE status IN ('recording','finalizing')
        AND (entry_id=? OR output_rel=?) LIMIT 1""", (entry['id'], entry['rel_dir'])).fetchone()
    if active or recording:
        raise ValueError('这条内容正在下载或录制，请在任务结束后再移入回收站')


def _inventory(entry, folder):
    files, signature = [], hashlib.sha256()
    def visit(path):
        info = _plain_node(path)
        if config.is_library_data_path(path):
            raise ValueError('目录内含有应用数据，无法移入回收站')
        relative = path.relative_to(folder).as_posix()
        signature.update(json.dumps([relative, info.st_mode, info.st_dev, info.st_ino,
                                     info.st_size, info.st_mtime_ns]).encode())
        if stat.S_ISDIR(info.st_mode):
            for child in sorted(path.iterdir(), key=lambda p: p.name):
                if child.name == 'entry.json' and path != folder:
                    raise ValueError('此目录内还有其他媒体存档，请先核对目录结构')
                visit(child)
        else:
            files.append({'path': relative, 'size': info.st_size})
    visit(folder)
    return {'entry_id': entry['id'], 'root': entry['root_path'], 'directory': entry['rel_dir'],
            'files': files}, signature.hexdigest()


def _plan(entry_id, database):
    with db.connect(database) as connection:
        entries = _members(connection, entry_id)
        other_count = len(catalog.other_archives(connection, entries[-1]))
        archives, fingerprints = [], []
        for entry in entries:
            folder = _folder(entry)
            _check_idle(connection, entry, folder)
            nested = connection.execute("""SELECT 1 FROM media_entries WHERE root_id=? AND id<>?
                AND substr(rel_dir,1,length(?)+1)=?||'/' LIMIT 1""",
                (entry['root_id'], entry['id'], entry['rel_dir'], entry['rel_dir'])).fetchone()
            if nested or any(Path(row['path']).resolve().is_relative_to(folder)
                             for row in connection.execute('SELECT path FROM media_roots')):
                raise ValueError('此目录内还有其他媒体条目或媒体根目录，请先核对')
            archive, signature = _inventory(entry, folder)
            entry['recycle_signature'] = signature
            archives.append(archive)
            fingerprints.append([entry['id'], entry['root_id'], str(folder), signature])
    revision = hashlib.sha256(json.dumps(fingerprints).encode()).hexdigest()
    files = [file for archive in archives for file in archive['files']]
    return entries, {'revision': revision, 'archives': archives, 'other_copies': other_count,
                     'file_count': len(files), 'bytes': sum(file['size'] for file in files)}


def preview(entry_id, *, database=None):
    return _plan(entry_id, database)[1]


def _mark(identity, state, database):
    with db.connect(database) as connection:
        connection.execute('UPDATE media_entries SET availability=? WHERE id=?', (state, identity))


def remove(entry_id, revision, *, database=None):
    with db.connect(database) as connection:
        roots = {row['root_id'] for row in _members(connection, entry_id)}
    with ExitStack() as locks:
        for root in sorted(roots):
            lock = catalog._scan_lock(root, database)
            if not lock.acquire(blocking=False):
                raise ValueError('媒体目录正在更新，请稍后重新查看清单')
            locks.callback(lock.release)
        entries, plan = _plan(entry_id, database)
        if revision != plan['revision'] or roots != {entry['root_id'] for entry in entries}:
            raise ValueError('文件清单已改变，请重新查看并确认')
        completed = 0
        for entry in entries:
            try:
                folder = _folder(entry)
                if len(entries) > 1 and _inventory(entry, folder)[1] != entry['recycle_signature']:
                    raise ValueError('清单中的文件发生变化')
                _mark(entry['id'], 'recycling', database)
                try:
                    recycle_bin.recycle(folder)
                except Exception:
                    if folder.is_dir():
                        _mark(entry['id'], 'present', database)
                    raise
                _mark(entry['id'], 'recycled', database)
                completed += 1
            except (OSError, ValueError, ImportError) as error:
                raise ValueError(f'已移入回收站 {completed} 个目录。请检查回收站、文件占用和磁盘状态，再重新查看清单') from error
        return {'ok': True, 'archives': completed, 'file_count': plan['file_count']}


def recover(*, database=None):
    """After a crash inspect only the folders with an unfinished operation."""
    with db.connect(database) as connection:
        rows = connection.execute("""SELECT e.id,e.rel_dir,r.path FROM media_entries e
            JOIN media_roots r ON r.id=e.root_id WHERE e.availability='recycling'""").fetchall()
        for row in rows:
            root = Path(row['path'])
            try:
                if not root.is_dir():
                    continue
                folder = catalog._safe(root.resolve(), row['rel_dir'])
                try:
                    _plain_node(folder)
                    state = 'present'
                except FileNotFoundError:
                    state = 'recycled'
                connection.execute('UPDATE media_entries SET availability=? WHERE id=?', (state, row['id']))
            except (OSError, ValueError):
                continue
