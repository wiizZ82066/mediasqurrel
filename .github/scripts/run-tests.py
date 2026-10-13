"""Offline Python regressions with disposable application paths on every host.

No browsers, live credentials, downloads, service launch or packaging are needed.
Keep the legacy test location covered until those useful regressions are moved.
"""
import argparse
import ipaddress
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


def _local_host(host):
    if host in (None, '', 'localhost'):
        return True
    if isinstance(host, bytes):
        host = host.decode('ascii', errors='replace')
    try:
        return ipaddress.ip_address(str(host)).is_loopback
    except ValueError:
        return False


def _offline_network(event, args):
    """Retain local HTTP/socket regression tests, reject real platform requests."""
    host = None
    if event == 'socket.getaddrinfo':
        host = args[0]
    elif event == 'socket.connect' and isinstance(args[1], tuple):
        host = args[1][0]
    if host is not None and not _local_host(host):
        raise RuntimeError('Offline regression tests cannot access external hosts')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--suite', choices=('business', 'legacy'), default='business')
    args = parser.parse_args()
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    with tempfile.TemporaryDirectory(prefix='mediasquirrel-tests-') as temporary:
        root = Path(temporary)
        # Discard inherited paths/login hints before importing any app module.
        for key in tuple(os.environ):
            if key.startswith('MS_'):
                del os.environ[key]
        os.environ.update(MS_HOME=str(root), MS_APP_DATA_DIR=str(root / 'data'),
                          MS_LIBRARY_DIR=str(root / 'library'),
                          MS_PATHS_FILE=str(root / 'unused-paths.json'),
                          PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
        sys.addaudithook(_offline_network)
        directory = ROOT / ('tests' if args.suite == 'business' else 'electron/tests')
        suite = unittest.defaultTestLoader.discover(str(directory), pattern='test*.py')
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        return int(not result.wasSuccessful())


if __name__ == '__main__':
    raise SystemExit(main())
