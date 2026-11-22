"""Machine-local QQ Mail preferences and delivery receipts, independent of removable media."""
from contextlib import contextmanager
import datetime as dt
import hashlib
import os
from pathlib import Path
import re
import sqlite3
import time

from . import config, db, mail_media

LIMIT = 32 * 1024**2  # Encoded message ceiling; honour a smaller advertised SMTP SIZE.
_ADDRESS = re.compile(r'[A-Za-z0-9][A-Za-z0-9._+-]{0,63}@(?:qq|foxmail)\.com', re.I)
_SCHEMA = """
CREATE TABLE IF NOT EXISTS preferences(
 id INTEGER PRIMARY KEY CHECK(id=1), sender TEXT DEFAULT '', recipient TEXT DEFAULT '',
 sealed BLOB, enabled INTEGER DEFAULT 0, since TEXT DEFAULT '');
INSERT OR IGNORE INTO preferences(id) VALUES(1);
CREATE TABLE IF NOT EXISTS delivery(
 id TEXT PRIMARY KEY, task_id TEXT NOT NULL, sample INTEGER NOT NULL, state TEXT NOT NULL,
 due REAL NOT NULL, updated REAL NOT NULL, done INTEGER DEFAULT 0, total INTEGER DEFAULT 0,
 attempts INTEGER DEFAULT 0, wire_limit INTEGER DEFAULT 0, fingerprint TEXT DEFAULT '', reason TEXT DEFAULT '',
 caption TEXT DEFAULT '');
CREATE INDEX IF NOT EXISTS mail_due ON delivery(state,due);
CREATE INDEX IF NOT EXISTS mail_recent ON delivery(updated DESC);
"""


@contextmanager
def connect():
    target = Path(config.DATA_DIR) / 'mail-delivery.sqlite'
    connection = sqlite3.connect(target, timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def initialize():
    with connect() as c:
        c.executescript(_SCHEMA)
        # A disconnect after DATA has no safe automatic retry.
        c.execute("UPDATE delivery SET state='uncertain', reason='发送时程序退出，请先核对收件箱。' WHERE state='sending'")
        c.execute("UPDATE delivery SET state='queued' WHERE state='preparing'")


def seal(code):
    try:
        import win32crypt
        return win32crypt.CryptProtectData(code.encode(), 'Media Squirrel / QQ Mail', None, None, None, 1)
    except Exception:
        raise ValueError('Windows 未能安全保存授权码，请在运行本程序的用户账户下重试。') from None


def unseal(blob):
    try:
        import win32crypt
        return win32crypt.CryptUnprotectData(blob, None, None, None, 1)[1].decode()
    except Exception:
        raise ValueError('当前 Windows 用户无法读取授权码，请在设置中重新填写。') from None


def preferences(c):
    return c.execute('SELECT * FROM preferences WHERE id=1').fetchone()


def public():
    with connect() as c:
        p = preferences(c)
        recent = [dict(r) for r in c.execute('''SELECT id,sample,state,done,total,reason,updated,caption FROM delivery
            ORDER BY updated DESC LIMIT 8''')]
        active = c.execute("SELECT count(*) FROM delivery WHERE state IN ('queued','preparing','sending')").fetchone()[0]
    return dict(sender=p['sender'], recipient=p['recipient'], has_code=bool(p['sealed']), enabled=bool(p['enabled']),
                supported=os.name == 'nt', recent=recent, active=active)


def utc_now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def save(sender, recipient, code, enabled):
    sender, recipient = sender.strip().lower(), recipient.strip().lower()
    if not _ADDRESS.fullmatch(sender) or not _ADDRESS.fullmatch(recipient):
        raise ValueError('请填写完整的 QQ 邮箱或 Foxmail 邮箱地址。')
    if code and not re.fullmatch(r'[A-Za-z0-9]{8,64}', code):
        raise ValueError('请填写发件邮箱生成的 SMTP 授权码。')
    with connect() as c:
        c.execute('BEGIN IMMEDIATE')
        old = preferences(c)
        if c.execute("SELECT 1 FROM delivery WHERE state IN ('preparing','sending') LIMIT 1").fetchone():
            raise ValueError('正在发送一封邮件，请在投递结束后更改邮箱设置。')
        blob = seal(code) if code else old['sealed'] if sender == old['sender'] else None
        if not blob:
            raise ValueError('请为发件邮箱填写 SMTP 授权码。')
        changed = (old['sender'], old['recipient']) != (sender, recipient)
        if changed or not enabled:
            states = "state IN ('queued','failed','uncertain')"
            scope = '' if changed else ' AND sample=0'
            c.execute(f"UPDATE delivery SET state='cancelled',reason='邮箱设置已更改' WHERE {states}{scope}")
        since = utc_now() if enabled and (not old['enabled'] or changed) else old['since']
        c.execute('UPDATE preferences SET sender=?,recipient=?,sealed=?,enabled=?,since=? WHERE id=1',
                  (sender, recipient, blob, int(enabled), since))
    return public()


def add(c, key, task_id, sample):
    now = time.time()
    c.execute("""INSERT OR IGNORE INTO delivery(id,task_id,sample,state,due,updated)
                 VALUES (?,?,?,'queued',?,?)""", (key, task_id, int(sample), now, now))


def capture(task):
    if not mail_media.eligible(task):
        return
    with connect() as c:
        c.execute('BEGIN IMMEDIATE')
        p = preferences(c)
        if p['enabled'] and (task.get('finished_at') or '') >= p['since']:
            add(c, task['id'], task['id'], False)


def recover():
    with connect() as c:
        p = preferences(c)
        if not p['enabled']:
            return
        since = p['since']
    # Read the task index once on startup, in batches; never walk media directories.
    with db.connect(existing=True) as source:
        rows = source.execute(f'SELECT id FROM tasks t WHERE {mail_media.ELIGIBLE_SQL} AND finished_at>=?', (since,))
        while batch := rows.fetchmany(100):
            with connect() as c:
                c.execute('BEGIN IMMEDIATE')
                current = preferences(c)
                if not current['enabled'] or current['since'] != since:
                    return
                for row in batch:
                    add(c, row['id'], row['id'], False)


def queue_test(task_id):
    with connect() as c:
        c.execute('BEGIN IMMEDIATE')
        p = preferences(c)
        if not p['sealed']:
            raise ValueError('请先保存发件邮箱、收件邮箱和授权码。')
        digest = hashlib.sha256('\0'.join((p['sender'], p['recipient'], task_id)).encode()).hexdigest()[:32]
        key = 'sample-' + digest
        add(c, key, task_id, True)
        c.execute("UPDATE delivery SET state='queued',due=?,reason='' WHERE id=? AND state='cancelled'", (time.time(), key))
        return key


def claim():
    with connect() as c:
        c.execute('BEGIN IMMEDIATE')
        row = c.execute("SELECT * FROM delivery WHERE state='queued' AND due<=? ORDER BY sample DESC,due LIMIT 1",
                        (time.time(),)).fetchone()
        if not row:
            due = c.execute("SELECT min(due) FROM delivery WHERE state='queued'").fetchone()[0]
            return None, max(0, due - time.time()) if due is not None else None
        c.execute("UPDATE delivery SET state='preparing', updated=? WHERE id=?", (time.time(), row['id']))
        return dict(row), dict(preferences(c))


def update(key, **values):
    # Keys are exclusively internal call-site constants.
    values['updated'] = time.time()
    with connect() as c:
        c.execute('UPDATE delivery SET ' + ','.join(f'{k}=?' for k in values) + ' WHERE id=?', (*values.values(), key))


def retry(key, confirm):
    with connect() as c:
        c.execute('BEGIN IMMEDIATE')
        row = c.execute('SELECT state,sample FROM delivery WHERE id=?', (key,)).fetchone()
        if not row or row['state'] not in ('failed','uncertain') or (not row['sample'] and not preferences(c)['enabled']):
            raise ValueError('这次投递当前不可重试。')
        if row['state'] == 'uncertain' and confirm is not True:
            raise ValueError('请先确认收件箱没有收到这一封，再重试。')
        c.execute("UPDATE delivery SET state='queued',due=?,updated=?,attempts=0,reason='' WHERE id=?",
                  (time.time(), time.time(), key))
