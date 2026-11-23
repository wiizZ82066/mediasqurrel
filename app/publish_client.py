"""Local launcher handoff. The HTTP request names a fingerprint, never an executable or a path."""
import json
from pathlib import Path
import urllib.error
import urllib.request

from . import config


def publish(port, identity, *, rollback=False):
    location = Path(config.DATA_DIR) / 'web-releases' / identity['source_id'] / 'launcher.key'
    credential = location.read_text(encoding='ascii').strip()
    body = {'action': 'rollback'} if rollback else {'code_signature': identity['code_signature']}
    request = urllib.request.Request(f'http://127.0.0.1:{port}/api/runtime/release',
        data=json.dumps(body).encode(), headers={'Content-Type': 'application/json', 'X-MS-Launcher': credential})
    # Refuse redirects so the private launcher header can never reach another origin.
    class LocalOnly(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args):
            return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), LocalOnly())
    try:
        with opener.open(request, timeout=240) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        try:
            detail = json.loads(error.read(4096)).get('detail')
        except (ValueError, OSError):
            detail = None
        raise RuntimeError(detail or '后台拒绝了这次发布') from None
