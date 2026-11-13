"""Select a database for the next process. Existing databases are only inspected here."""
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import threading
import uuid

from . import config, db
from .archive import atomic_write_text

_lock = threading.Lock()


def inspect_database(file, *, quick_check=False):
    if not file.is_file() or file.is_symlink() or file.resolve() != file:
        raise ValueError('找不到可用的 Media Squirrel 数据库，请检查文件与磁盘。')
    try:
        with closing(sqlite3.connect(file.as_uri() + '?mode=ro', uri=True, timeout=4)) as c:
            c.execute('PRAGMA query_only=ON')
            version = c.execute('PRAGMA user_version').fetchone()[0]
            if not 1 <= version <= db._migrations()[-1][0]:
                raise ValueError('数据库版本不受当前程序支持。')
            row = c.execute('SELECT name FROM schema_migrations WHERE version=1').fetchone()
            if row != ('subscription-history',) or not c.execute("SELECT 1 FROM sqlite_master WHERE name='subscriptions' AND type='table'").fetchone():
                raise ValueError('所选文件并非 Media Squirrel 数据库。')
            if quick_check and c.execute('PRAGMA quick_check').fetchone() != ('ok',):
                raise ValueError('所选数据库未通过完整性检查。')
            return version
    except sqlite3.Error as exc:
        raise ValueError('无法读取 Media Squirrel 数据库，请确认文件完整且可访问。') from exc


def _folder(raw):
    if config.database_pinned_by_environment():
        raise ValueError('启动参数已指定数据目录。请取消该指定后再从设置中切换。')
    if not isinstance(raw, str) or not raw.strip() or len(raw) > 4096:
        raise ValueError('请选择一个本机文件夹。')
    candidate = Path(raw.strip()).expanduser()
    if not candidate.is_absolute() or str(candidate).startswith(('\\\\', '//')) or '..' in candidate.parts:
        raise ValueError('请选择本机磁盘上的绝对路径。')
    try:
        folder = candidate.resolve(strict=True)
    except FileNotFoundError:
        raise ValueError('该文件夹不存在，请先创建或重新选择。') from None
    if not folder.is_dir() or folder == folder.parent:
        raise ValueError('请选择独立的数据库文件夹。')
    active = Path(config.DB_PATH).resolve().parent
    if folder != active:
        media = Path(config.LIBRARY_ROOT).resolve()
        old_data = Path(config._store[0]).parent if config._store else None
        if folder.is_relative_to(media) and folder != old_data:
            raise ValueError('请选择媒体库以外的数据库文件夹。')
        if folder.is_relative_to(Path(config.SOURCE_DIR).resolve()):
            raise ValueError('请选择项目源码以外的数据库文件夹。')
    return folder


def _inspect(raw, *, verify=False):
    folder = _folder(raw)
    target = folder / 'app.db'
    if target.exists():
        version = inspect_database(target, quick_check=verify)
        return {'directory': str(folder), 'database': str(target), 'kind': 'existing', 'schema_version': version}
    if any(folder.iterdir()):
        raise ValueError('该目录已有其他文件。请选择空文件夹，或选择含有 Media Squirrel app.db 的文件夹。')
    return {'directory': str(folder), 'database': str(target), 'kind': 'new', 'schema_version': None}


def preview(directory):
    return _inspect(directory, verify=True)


def status():
    active = str(Path(config.DB_PATH).resolve().parent)
    configured = config._read_paths().get('database_directory')
    next_dir = Path(configured).resolve() if configured else Path(config.DEFAULT_DB_PATH).resolve().parent
    if config.database_pinned_by_environment():
        next_dir = Path(active)
    return {'directory': active, 'next_directory': str(next_dir), 'restart_required': next_dir != Path(active),
            'fallback': config.DATABASE_FALLBACK and str(next_dir) == config._database_override}


def select(directory, expected_kind):
    if expected_kind not in ('new', 'existing'):
        raise ValueError('请先预检并确认选中的目录。')
    with _lock:
        setup = config._read_paths()
        if setup.get('pending_transition') or config.MAINTENANCE_ACTIVE:
            raise RuntimeError('有其他数据操作正在进行，请稍后重试。')
        info = _inspect(directory, verify=True)
        if info['kind'] != expected_kind:
            raise ValueError('所选目录已发生变化，请重新选择。')
        target = Path(info['database'])
        if expected_kind == 'new':
            # Publish a fully initialized SQLite file without replacing any
            # file created by another program after the directory preview.
            temp = target.with_name('.ms-' + uuid.uuid4().hex + '.db')
            try:
                db.migrate(temp)
                with closing(sqlite3.connect(temp)) as c:
                    if c.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0]:
                        raise RuntimeError('新数据库初始化未完成，请重试。')
                inspect_database(temp, quick_check=True)
                if os.name == 'nt':
                    temp.rename(target)  # Windows refuses an existing destination.
                else:
                    os.link(temp, target)
            finally:
                for suffix in ('', '-wal', '-shm'):
                    Path(str(temp) + suffix).unlink(missing_ok=True)
        # Selecting the default location restores the original pairing and its
        # automatic local fallback when the preferred drive is disconnected.
        if target == Path(config.DEFAULT_DB_PATH).resolve():
            setup.pop('database_directory', None)
        else:
            setup['database_directory'] = info['directory']
        atomic_write_text(config.PATHS_FILE, json.dumps(setup, ensure_ascii=False, indent=2) + '\n')
        return {'directory': info['directory'], 'kind': expected_kind,
                'restart_required': target != Path(config.DB_PATH).resolve()}


def pick_folder():
    import tkinter as tk
    from tkinter import filedialog
    win = tk.Tk()
    try:
        win.withdraw()
        win.attributes('-topmost', True)
        return filedialog.askdirectory(parent=win, title='选择数据库目录', mustexist=True,
            initialdir=str(Path(config.DB_PATH).parent))
    finally:
        win.destroy()
