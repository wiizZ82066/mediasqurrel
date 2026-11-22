"""Build mail attachments exclusively from one verified, successful media download."""
from dataclasses import dataclass
from collections import Counter
from contextlib import contextmanager
import hashlib
import json
import math
import mimetypes
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from fastapi import HTTPException

from . import archive, catalog, config, db, live_merge, live_recorder, task_files
from .redaction import redact_text
from .security import MEDIA_EXTENSIONS, media_path

PLATFORMS = {'weibo': '微博', 'douyin': '抖音'}
ELIGIBLE_SQL = """t.status='success' AND t.script_id IN ('weibo','douyin') AND t.deleted=0
    AND json_extract(t.metadata_json,'$.live') IS NULL
    AND coalesce(json_extract(t.metadata_json,'$.diagnostic'),0)=0"""


def eligible(task):
    meta = task.get('metadata') or {}
    return (task.get('status') == 'success' and task.get('script_id') in PLATFORMS
            and not task.get('hidden') and meta.get('live') is None and not meta.get('diagnostic'))


def task_by_id(identity):
    from .task_store import TaskStore
    with db.connect(existing=True) as c:
        row = c.execute(f'SELECT t.* FROM tasks t WHERE t.id=? AND {ELIGIBLE_SQL}', (identity,)).fetchone()
    return TaskStore._decode(row)


def clean_line(value, length):
    return ' '.join(str(value or '').split())[:length]


@dataclass(frozen=True)
class Work:
    author: str
    platform: str
    text: str
    paths: tuple

    @property
    def title(self):
        return clean_line(self.text, 80) or '无标题作品'

    def summary(self):
        return dict(author=self.author, platform=self.platform, title=self.title, count=len(self.paths),
                    bytes=sum(p.stat().st_size for p in self.paths))


def resolve(task):
    if not task or not eligible(task):
        raise ValueError('只能发送已成功下载的普通作品。')
    with db.connect(existing=True) as c:
        row = c.execute('''SELECT e.*,r.path root_path,
                EXISTS(SELECT 1 FROM live_recordings l WHERE l.entry_id=e.id) recording
            FROM task_media m JOIN media_entries e ON e.id=m.entry_id JOIN media_roots r ON r.id=e.root_id
            WHERE m.task_id=?''', (task['id'],)).fetchone()
        if row:
            if row['availability'] != 'present' or row['recording'] or row['platform'] != task['script_id']:
                raise ValueError('这条作品已移除、属于录播或与任务不匹配。')
            folder = Path(media_path(row['rel_dir'], root=row['root_path']))
            assets = [a[0] for a in c.execute('SELECT rel_path FROM media_assets WHERE entry_id=? ORDER BY rel_path', (row['id'],))]
        else:
            folder = Path(task_files.output_directory(task))
            assets = []
    marker_path = folder / archive.MARKER_NAME
    if not marker_path.resolve().is_relative_to(folder) or not (folder / 'context.md').resolve().is_relative_to(folder):
        raise ValueError('作品目录包含指向其他位置的文件。')
    if marker_path.exists():
        marker = task_files._bounded_json(marker_path) or {}
        if (marker.get('schema_version') != 1 or marker.get('status') != 'complete'
                or marker.get('platform') != task['script_id'] or not marker.get('item_id')
                or (row and row['item_id'] and str(marker['item_id']) != str(row['item_id']))
                or (task.get('content_key') and task['content_key'] != f"{task['script_id']}:{marker['item_id']}")):
            raise ValueError('作品文件尚未完整保存，或与下载记录不匹配。')
        inventory = marker.get('files') or []
        sizes = {item['path']: item['size'] for item in inventory if isinstance(item, dict)}
        assets = list(dict.fromkeys([*(marker.get('media_order') or []), *sizes]))
    else:
        sizes = {}
        if not row:
            raise ValueError('旧作品缺少可核验的媒体清单，请先为作品建立索引。')
    paths = []
    for rel in assets:
        if sizes and rel not in sizes:
            continue
        relpath = Path(rel)
        if relpath.suffix.lower() not in MEDIA_EXTENSIONS or any(part.startswith('.') and part != '..' for part in relpath.parts):
            continue  # Hidden archive originals, browser data and sidecars are never mailed.
        path = Path(media_path(rel, root=folder, file_only=True))
        if sizes and path.stat().st_size != sizes[rel]:
            raise ValueError('作品文件大小已经改变，请核对下载产物。')
        paths.append(path)
    if not paths:
        raise ValueError('没有可发送的图片或视频。')
    meta, body = catalog._context(folder)
    author = clean_line(meta.get('作者') or (row['author'] if row else '') or folder.parent.name, 100)
    description = meta.get('视频标题') or body or (row['text_preview'] if row else '')
    return Work(author, PLATFORMS[task['script_id']], redact_text(description or '', diagnostic=True), tuple(paths))


def sample():
    """Prefer a recent small work that fits into a single mail, without crawling the library."""
    with db.connect(existing=True) as c:
        rows = c.execute(f'''SELECT t.id FROM tasks t JOIN task_media m ON m.task_id=t.id
            JOIN media_entries e ON e.id=m.entry_id
            WHERE {ELIGIBLE_SQL} AND e.availability='present' AND e.file_count>0
            AND NOT EXISTS(SELECT 1 FROM live_recordings l WHERE l.entry_id=e.id)
            ORDER BY (e.size BETWEEN 1 AND 8388608) DESC,t.created_at DESC LIMIT 40''').fetchall()
    for row in rows:
        try:
            return dict(task_id=row[0], **resolve(task_by_id(row[0])).summary())
        except (ValueError, HTTPException, OSError, KeyError):
            continue
    raise ValueError('暂未找到可发送的已下载作品。')


@dataclass(frozen=True)
class Attachment:
    path: Path
    name: str
    offset: int
    size: int
    mime: str

    def read(self):
        with self.path.open('rb') as stream:
            stream.seek(self.offset)
            data = stream.read(self.size)
        if len(data) != self.size:
            raise ValueError('读取期间文件发生变化，请核对作品。')
        return data


def _scratch_root():
    cache = Path(config.CACHE_DIR).resolve()
    root = cache / 'mail-attachments'
    if root.resolve() != root:
        raise ValueError('邮件临时目录指向其他位置，请检查缓存目录。')
    return root


def release_interrupted():
    root = _scratch_root()
    if root.is_dir():
        for path in root.glob('job-*'):
            if (path.resolve().parent == root and path.resolve() == path and path.is_dir()
                    and (path / '.mail-work').is_file()):
                shutil.rmtree(path)


@contextmanager
def workspace():
    root = _scratch_root()
    root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='job-', dir=root) as scratch:
        (Path(scratch) / '.mail-work').touch()
        yield scratch


def video_parts(path, budget, scratch):
    """Remux at keyframes. If a GOP cannot fit, callers retain every original byte in volumes."""
    if shutil.disk_usage(scratch).free < path.stat().st_size + 64 * 1024**2:
        return []
    try:
        duration, _ = live_merge.probe(path)
        interval = duration * budget / path.stat().st_size * 0.77
        for attempt in range(3):
            dest = scratch / str(attempt)
            dest.mkdir()
            result = subprocess.run([live_recorder.ffmpeg_exe(), '-nostdin', '-hide_banner', '-loglevel', 'error',
                '-protocol_whitelist', 'file', '-i', str(path), '-map', '0:v:0', '-map', '0:a?', '-c', 'copy',
                '-f', 'segment', '-segment_time', str(max(0.2, interval)), '-reset_timestamps', '1',
                '-segment_format_options', 'movflags=+faststart', str(dest / '%06d.mp4')],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180, creationflags=live_recorder.CREATE_HIDDEN)
            parts = sorted(dest.glob('*.mp4'))
            if result.returncode == 0 and len(parts) > 1 and all(0 < p.stat().st_size <= budget for p in parts):
                return parts
            shutil.rmtree(dest)
            interval /= 2
            if result.returncode:
                break
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return []


def batches(work, wire_limit, scratch):
    # MIME base64 expands in 76-character lines. Also reserve headers and the bounded mail body.
    budget = (wire_limit - 192 * 1024) * 70 // 100
    if budget < 64 * 1024:
        raise ValueError('邮箱公布的邮件容量太小，无法可靠投递附件。')
    groups, current, used = [], [], 0
    fingerprint = hashlib.sha256()
    volumes = set()
    inputs = list(work.paths)
    text_path = None
    if len(work.text) > 4000:
        text_path = Path(scratch) / '作品正文.txt'
        text_path.write_bytes(work.text.encode('utf-8'))
        inputs.insert(0, text_path)
    names = Counter(p.name.casefold() for p in inputs)
    for i, path in enumerate(inputs, 1):
        stat = path.stat()
        name = clean_line(path.name, 150)
        if names[path.name.casefold()] > 1:
            name = f'{i:02d}-{name}'
        revision = hashlib.sha256(work.text.encode()).hexdigest() if path == text_path else stat.st_mtime_ns
        fingerprint.update(json.dumps([name, stat.st_size, revision], ensure_ascii=False).encode())
        mime = mimetypes.guess_type(name)[0] or 'application/octet-stream'
        if stat.st_size <= budget:
            parts = [Attachment(path, name, 0, stat.st_size, mime)]
        else:
            segments = []
            if mime.startswith('video/'):
                dest = Path(scratch) / str(i)
                dest.mkdir()
                segments = video_parts(path, budget, dest)
            if segments:
                parts = [Attachment(p, f'{Path(name).stem}-分段{n:03d}.mp4', 0, p.stat().st_size, 'video/mp4')
                         for n, p in enumerate(segments, 1)]
            else:
                volumes.add(name)
                parts = [Attachment(path, f'{name}.{n + 1:03d}', n * budget,
                                    min(budget, stat.st_size - n * budget), 'application/octet-stream')
                         for n in range(math.ceil(stat.st_size / budget))]
        for part in parts:
            if current and (used + part.size > budget or len(current) >= 20):
                groups.append(current)
                current, used = [], 0
            current.append(part)
            used += part.size
            fingerprint.update(json.dumps([part.name, part.size]).encode())
    if current:
        groups.append(current)
    return groups, fingerprint.hexdigest(), volumes
