"""One private pipe per web generation. The child never starts the application lifespan."""
import hashlib
import json
import multiprocessing
import re
import threading


PROTOCOL = 1
LIMIT = 4 * 1024 * 1024
REQUIRED = {'/api/home': 'home', '/api/scripts': 'scripts', '/api/activity': 'activity'}


class WebWorkerError(RuntimeError):
    pass


def _encode(value):
    data = json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()
    if len(data) > LIMIT:
        raise ValueError('网页接口响应超过大小限制')
    return data


def _serve(pipe, code):
    try:
        namespace = {'__name__': 'media_squirrel_web_api'}
        exec(compile(code, '<published web API>', 'exec'), namespace)
        routes = namespace['ROUTES']
        if namespace['PROTOCOL'] != PROTOCOL or not isinstance(routes, dict) or len(routes) > 128:
            raise ValueError('网页接口协议不兼容')
        if any(routes.get(path) != source for path, source in REQUIRED.items()):
            raise ValueError('网页接口缺少已有入口')
        for path, source in routes.items():
            if source not in REQUIRED.values() or (path not in REQUIRED and not re.fullmatch(r'/api/web/[a-z0-9-]+', path)):
                raise ValueError('网页接口路径或数据源不受支持')
        render = namespace['render']
        if not callable(render):
            raise ValueError('网页接口入口无效')
        pipe.send_bytes(_encode(routes))
        while True:
            path, data = json.loads(pipe.recv_bytes(LIMIT))
            try:
                value = _encode({'value': render(path, data)})
            except Exception:
                value = b'{}'
            pipe.send_bytes(value)
    except (EOFError, BrokenPipeError, OSError, ValueError, TypeError, KeyError, SyntaxError):
        pass
    finally:
        pipe.close()


class WebWorker:
    def __init__(self, code, expected_hash):
        if hashlib.sha256(code).hexdigest() != expected_hash:
            raise ValueError('网页接口文件校验失败')
        ctx = multiprocessing.get_context('spawn')
        self.pipe, child = ctx.Pipe()
        self.lock = threading.Lock()
        self.process = ctx.Process(target=_serve, args=(child, code), name='Media Squirrel Web', daemon=True)
        try:
            self.process.start()
            child.close()
            self.routes = self.receive(12)
        except BaseException:
            child.close()
            self.close()
            raise

    def receive(self, timeout):
        if not self.pipe.poll(timeout):
            raise WebWorkerError('网页接口进程响应超时')
        try:
            return json.loads(self.pipe.recv_bytes(LIMIT))
        except (EOFError, OSError, ValueError) as error:
            raise WebWorkerError('网页接口进程未能正常响应') from error

    def call(self, path, data):
        payload = _encode([path, data])
        with self.lock:
            if not self.process.is_alive():
                raise WebWorkerError('网页接口进程已退出')
            try:
                self.pipe.send_bytes(payload)
                result = self.receive(5)
            except (OSError, RuntimeError) as error:
                self.close()
                raise WebWorkerError('网页接口连接已断开') from error
            if 'value' not in result:
                raise WebWorkerError('网页接口处理失败')
            return result['value']

    def close(self):
        self.pipe.close()
        if self.process.pid is not None:
            self.process.join(.5)
            if self.process.is_alive():
                self.process.terminate()
                self.process.join(3)


def check_contract(worker):
    samples = {
        'home': {'summary': {'downloads': {'running': 0, 'queued': 0, 'items': []},
                             'live': {'enabled': 0}, 'scans': {'enabled': 0, 'next': None}, 'recent': []},
                 'active': 0, 'monitor': None, 'scanning': 0, 'progress': None, 'updated_at': 'test'},
        'scripts': {'scripts': [{'id': 'fixture', 'available': True}]},
        'activity': {'counts': {'recording': 0, 'running': 0, 'queued': 0, 'scanning': 0}, 'recordings': []},
    }
    schemas = {
        '/api/home': {'downloads': {'running': int, 'queued': int, 'items': list},
                      'scans': {'active': int, 'enabled': int, 'next': object, 'progress': object},
                      'live': {'active': int, 'enabled': int, 'current': object}, 'recent': list, 'updated_at': str},
        '/api/activity': {'recording': int, 'running': int, 'queued': int, 'scanning': int, 'recordings': list},
        '/api/scripts': list,
    }

    def matches(value, shape):
        if isinstance(shape, dict):
            return isinstance(value, dict) and all(k in value and matches(value[k], item) for k, item in shape.items())
        return isinstance(value, shape)

    for path, source in worker.routes.items():
        result = worker.call(path, samples[source])
        if path in schemas and not matches(result, schemas[path]):
            raise ValueError(f'网页接口 {path} 的响应不兼容')
        if path == '/api/scripts' and (not result or not matches(result[0], {'id': str, 'available': bool})):
            raise ValueError('网页接口的脚本清单不兼容')
