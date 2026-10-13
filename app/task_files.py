"""Resolve persisted task outputs without trusting arbitrary shell paths."""
import json
import os
from pathlib import Path

from fastapi import HTTPException

from . import config
from .security import media_path


def _bounded_json(path):
    if not path.is_file() or path.stat().st_size > 256 * 1024:
        return None
    try:
        with path.open('rb') as stream:
            raw = stream.read(256 * 1024 + 1)
        if len(raw) > 256 * 1024:
            return None
        value = json.loads(raw.decode('utf-8'))
        return value if isinstance(value, dict) else None
    except (ValueError, UnicodeError):
        return None


def _has_output(folder, task):
    marker_path = folder / 'entry.json'
    if marker_path.exists():
        if not marker_path.resolve().is_relative_to(folder):
            return False
        marker = _bounded_json(marker_path)
        if not marker or marker.get('schema_version') != 1 or marker.get('status') not in ('partial', 'complete'):
            return False
        if marker.get('platform') != task.get('script_id') or not marker.get('entry_id') or not marker.get('item_id'):
            return False
        source = (task.get('metadata') or {}).get('subscription') or {}
        if source and (source.get('platform') != marker['platform'] or
                       str(source.get('item_id')) != str(marker['item_id'])):
            return False
        content_key = task.get('content_key')
        if content_key and content_key != f"{marker['platform']}:{marker['item_id']}":
            return False
        # The standard archive name independently ties this marker to the folder.
        return folder.name.endswith(f"_{marker['platform']}_{marker['item_id']}")
    # Legacy downloads used a context.md without an entry marker. Its contents
    # need not be read, displayed or interpreted to locate the containing folder.
    context = folder / 'context.md'
    return (context.is_file() and context.resolve().is_relative_to(folder)
            and context.stat().st_size > 0)


def output_directory(task):
    """Use recorded output only; an existing requested destination is insufficient."""
    output = task.get('output_dir')
    relative = task.get('output_rel')
    if not output and not relative:
        raise HTTPException(404, '此任务尚未记录可定位的下载产物；排队或尚未生成文件时不可定位')
    requested = (task.get('params') or {}).get('out')
    if requested and (not isinstance(requested, str) or not os.path.isabs(requested)):
        raise HTTPException(403, '任务输出根目录无效，不能定位此路径')
    root = Path(requested or config.LIBRARY_ROOT).resolve()
    if output:
        if not isinstance(output, str) or not os.path.isabs(output):
            raise HTTPException(403, '任务产物路径无效')
        folder = Path(output).resolve()
    else:
        if not isinstance(relative, str):
            raise HTTPException(403, '任务产物路径无效')
        folder = Path(media_path(relative, root=config.LIBRARY_ROOT))
    if folder == root or not folder.is_relative_to(root):
        raise HTTPException(403, '任务产物不在该任务的输出目录内，不能定位')
    # Apply the same traversal/private-segment boundary as indexed media after
    # validating absolute persisted paths against their original destination.
    folder = Path(media_path(folder.relative_to(root).as_posix(), root=root))
    if not folder.is_dir():
        raise HTTPException(404, '任务产物目录已移动或不存在，请更新索引或检查磁盘')
    if not _has_output(folder, task):
        raise HTTPException(404, '未找到可核验的下载产物；可在下载生成文件后重试或先建立媒体索引')
    return str(folder)
