"""Immutable published pages and shared Vite assets, kept outside the media library."""
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import re
import shutil
import tempfile

from . import runtime
from .archive import atomic_write_text


def digest(data):
    return hashlib.sha256(data).hexdigest()


def key(value):
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{64}', value):
        raise ValueError('网页版本标识无效')
    return value


def child(base, name):
    if (not isinstance(name, str) or not name or '\\' in name or ':' in name or PureWindowsPath(name).anchor
            or any(not part or part.startswith('.') for part in name.split('/'))):
        raise ValueError('网页资源路径无效')
    path = base / name
    if path.resolve() != base.resolve() / name:
        raise ValueError('网页资源不能使用目录外的链接')
    return path


@dataclass
class Bundle:
    version: str
    folder: Path
    shared: Path
    files: dict
    code: bytes
    api_hash: str
    html: bytes = b''

    def public(self, name):
        if name.startswith('assets/'):
            # Hashed URLs stay available to old tabs after their API process exits.
            path = child(self.shared, name)
        elif name in self.files:
            path = child(self.folder / 'public', name)
        else:
            return None
        return path if path.is_file() else None


def load(base, version):
    folder = child(base, 'bundles/' + key(version))
    meta = json.loads(child(folder, 'manifest.json').read_bytes())
    if runtime.web_signature(key(meta['frontend']), key(meta['api'])) != version:
        raise ValueError('网页版本与清单不一致')
    code = child(folder, 'api.py').read_bytes()
    if digest(code) != meta['api']:
        raise ValueError('网页接口校验失败')
    files = meta['files']
    if not isinstance(files, dict) or 'index.html' not in files or len(files) > 10000:
        raise ValueError('网页资源清单不完整')
    bundle = Bundle(version, folder, base, files, code, meta['api'])
    for name, expected in files.items():
        path = bundle.public(name)
        if path is None or digest(path.read_bytes()) != key(expected):
            raise ValueError('网页资源校验失败')
    html = bundle.public('index.html').read_bytes()
    tag = f'<meta name="media-squirrel-web" content="{version}">'.encode('ascii')
    bundle.html = html.replace(b'</head>', tag + b'</head>', 1) if b'</head>' in html else tag + html
    return bundle


def stage(base, source, identity):
    """Publish no pointer here. A failed build cannot replace the active HTML or API."""
    base.mkdir(parents=True, exist_ok=True)
    target = child(base, 'bundles/' + key(identity['web']))
    if target.is_dir():
        return load(base, identity['web'])
    frontend = source / 'frontend'
    dist = frontend / 'dist'
    runtime.ensure_frontend(frontend=frontend)
    if (dist / '.source-hash').read_text().strip() != identity['frontend']:
        raise ValueError('构建期间源码发生变化，请重新发布')
    code = (source / 'app' / 'web_api.py').read_bytes()
    if digest(code) != identity['api']:
        raise ValueError('发布期间网页接口发生变化，请重新发布')
    files = json.loads((dist / '.build-manifest.json').read_bytes())
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=base, prefix='stage-') as temporary:
        work = Path(temporary)
        bundle = work / 'bundle'
        bundle.mkdir()
        for name, expected in files.items():
            src = child(dist, name)
            # Match the public surface of the source launcher. Never serve the manifest or Python source.
            if not (name in ('index.html', 'squirrel.svg') or name.startswith(('assets/', 'licenses/'))):
                raise ValueError('构建含未声明的公开资源')
            shared = name.startswith('assets/')
            dest = child(base, name) if shared else child(bundle / 'public', name)
            if shared and dest.exists():
                if digest(dest.read_bytes()) != key(expected):
                    raise ValueError('同名网页资源内容不同，已保留当前版本')
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            pending = work / 'asset.tmp'
            shutil.copyfile(src, pending)
            if digest(pending.read_bytes()) != key(expected):
                raise ValueError('构建文件在发布期间发生变化')
            os.replace(pending, dest)
        (bundle / 'api.py').write_bytes(code)
        atomic_write_text(bundle / 'manifest.json', json.dumps({
            'frontend': identity['frontend'], 'api': identity['api'], 'files': files}, sort_keys=True))
        os.replace(bundle, target)
    return load(base, identity['web'])
