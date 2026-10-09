"""Local calendar schedules. UTC cursors survive sleep and process restarts.

Calendar counts refer to intended occurrences, including occurrences coalesced
after downtime. An ambiguous local time runs at its first occurrence; a missing
DST time is shifted forward by the transition gap. Monthly anchors clamp to the
last day of shorter months without drifting the original anchor.
"""
import calendar
import datetime as dt
import json
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import db
from .redaction import redact_text

UTC = dt.timezone.utc
DEFAULT_ZONE = 'Asia/Shanghai'
SCHEMA = """
ALTER TABLE subscriptions ADD COLUMN avatar_source TEXT NOT NULL DEFAULT '';
CREATE TABLE IF NOT EXISTS subscription_schedules (
 id TEXT PRIMARY KEY, sub_id INTEGER NOT NULL, kind TEXT NOT NULL,
 plan_json TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
 next_due TEXT, last_due TEXT, retry_due TEXT, retry_count INTEGER NOT NULL DEFAULT 0,
 last_status TEXT NOT NULL DEFAULT '', last_error TEXT NOT NULL DEFAULT '',
 revision INTEGER NOT NULL DEFAULT 1
);
CREATE UNIQUE INDEX IF NOT EXISTS subscription_interval_schedule ON subscription_schedules(sub_id)
 WHERE kind='interval';
CREATE INDEX IF NOT EXISTS subscription_schedule_due ON subscription_schedules(enabled,next_due);
CREATE INDEX IF NOT EXISTS subscription_schedule_retry ON subscription_schedules(enabled,retry_due);
CREATE TABLE IF NOT EXISTS schedule_runs (
 id TEXT PRIMARY KEY, sub_id INTEGER NOT NULL, schedule_ids_json TEXT NOT NULL,
 due_at TEXT NOT NULL, trigger TEXT NOT NULL, status TEXT NOT NULL,
 started_at TEXT NOT NULL, finished_at TEXT, error TEXT NOT NULL DEFAULT '', scan_id TEXT
);
CREATE INDEX IF NOT EXISTS schedule_runs_sub ON schedule_runs(sub_id,started_at DESC,id DESC);
CREATE UNIQUE INDEX IF NOT EXISTS schedule_run_active ON schedule_runs(sub_id) WHERE status='running';
CREATE TABLE IF NOT EXISTS subscription_coverage (
 sub_id INTEGER PRIMARY KEY, last_success_at TEXT, covered_until TEXT,
 coverage_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS subscription_scan_options (
 sub_id INTEGER PRIMARY KEY, limits_json TEXT NOT NULL DEFAULT '{}'
);
"""


def utc(value=None, zone=DEFAULT_ZONE):
    if value is None:
        return dt.datetime.now(UTC)
    if isinstance(value, str):
        value = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    if value.tzinfo is None:
        value = value.replace(tzinfo=ZoneInfo(zone))
    return value.astimezone(UTC)


def stamp(value=None):
    return utc(value).isoformat(timespec='seconds')


def _integer(value, name, low, high):
    if isinstance(value, bool):
        raise ValueError(f'{name}必须是整数')
    try:
        integer = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f'{name}必须是整数') from error
    if str(integer) != str(value) or not low <= integer <= high:
        raise ValueError(f'{name}须介于{low}和{high}之间')
    return integer


def normalize_plan(source, *, now=None):
    if not isinstance(source, dict):
        raise ValueError('扫描计划必须为对象')
    kind = source.get('kind', 'daily')
    if kind not in ('interval', 'daily', 'weekly', 'dates', 'repeat'):
        raise ValueError('不支持的扫描计划类型')
    zone = str(source.get('timezone') or DEFAULT_ZONE)
    try:
        tz = ZoneInfo(zone)
    except ZoneInfoNotFoundError as error:
        raise ValueError('未知时区或缺少tzdata时区依赖') from error
    enabled = source.get('enabled', True)
    if not isinstance(enabled, bool):
        raise ValueError('计划启用状态必须是布尔值')
    plan = {'kind': kind, 'timezone': zone, 'enabled': enabled}
    if kind == 'interval':
        plan['minutes'] = _integer(source.get('minutes', 30), '扫描间隔', 5, 525600)
        return plan
    start = dt.date.fromisoformat(str(source.get('start_date') or utc(now).astimezone(tz).date()))
    plan['start_date'] = start.isoformat()
    times = source.get('times') or ['09:00']
    if not isinstance(times, list) or not 1 <= len(times) <= 24:
        raise ValueError('每天需设置1至24个时间点')
    normalized = []
    for value in times:
        try:
            hour, minute = str(value).split(':')
            normalized.append(dt.time(int(hour), int(minute)).strftime('%H:%M'))
        except (ValueError, TypeError) as error:
            raise ValueError('时间点须为HH:MM') from error
    plan['times'] = sorted(set(normalized))
    count = source.get('count')
    plan['count'] = None if count in (None, '') else _integer(count, '触发次数', 1, 10000)
    end = source.get('end_date')
    plan['end_date'] = dt.date.fromisoformat(str(end)).isoformat() if end else None
    if end and plan['end_date'] < plan['start_date']:
        raise ValueError('结束日期不能早于开始日期')
    if kind == 'weekly':
        values = source.get('weekdays')
        if not isinstance(values, list) or not values:
            raise ValueError('请至少选择一个星期')
        plan['weekdays'] = sorted({_integer(value, '星期', 0, 6) for value in values})
    if kind == 'dates':
        values = source.get('dates')
        if not isinstance(values, list) or not 1 <= len(values) <= 1000:
            raise ValueError('请设置1至1000个明确日期')
        plan['dates'] = sorted({dt.date.fromisoformat(str(value)).isoformat() for value in values})
    if kind == 'repeat':
        unit = source.get('unit', 'days')
        if unit not in ('days', 'weeks', 'months'):
            raise ValueError('重复单位须为days、weeks或months')
        plan.update(unit=unit, every=_integer(source.get('every', 1), '重复间隔', 1, 1200))
    return plan


def local_instant(day, time_string, zone):
    naive = dt.datetime.combine(day, dt.time.fromisoformat(time_string))
    tz = ZoneInfo(zone)
    first = naive.replace(tzinfo=tz, fold=0).astimezone(UTC)
    second = naive.replace(tzinfo=tz, fold=1).astimezone(UTC)
    valid = [value for value in (first, second) if value.astimezone(tz).replace(tzinfo=None) == naive]
    if valid:
        return min(valid)
    # Forward round trip is the same local minute after the skipped DST gap.
    return min(value for value in (first, second) if value.astimezone(tz).replace(tzinfo=None) > naive)


def next_occurrence(plan, after):
    after = utc(after)
    if plan['kind'] == 'interval':
        return after + dt.timedelta(minutes=plan['minutes'])
    start = dt.date.fromisoformat(plan['start_date'])
    end = dt.date.fromisoformat(plan['end_date']) if plan.get('end_date') else dt.date.max
    local_after = after.astimezone(ZoneInfo(plan['timezone'])).date()
    count, produced = plan.get('count'), 0
    kind = plan['kind']
    if kind == 'dates':
        days = [dt.date.fromisoformat(value) for value in plan['dates']]
    else:
        def candidates():
            if kind == 'repeat':
                every, unit = plan['every'], plan['unit']
                if unit == 'months':
                    difference = (local_after.year - start.year) * 12 + local_after.month - start.month
                    index = 0 if count else max(0, difference // every - 1)
                    for number in range(index, index + 120000):
                        month = start.year * 12 + start.month - 1 + number * every
                        year, month = divmod(month, 12)
                        if year > 9999:
                            return
                        month += 1
                        yield dt.date(year, month, min(start.day, calendar.monthrange(year, month)[1]))
                else:
                    step = every * (7 if unit == 'weeks' else 1)
                    index = 0 if count else max(0, (local_after - start).days // step - 1)
                    for number in range(index, index + 120000):
                        try:
                            yield start + dt.timedelta(days=number * step)
                        except OverflowError:
                            return
            else:
                day = start if count else max(start, local_after - dt.timedelta(days=1))
                for _ in range(366000):
                    if kind != 'weekly' or day.weekday() in plan['weekdays']:
                        yield day
                    if day == dt.date.max:
                        return
                    day += dt.timedelta(days=1)
        days = candidates()
    for day in days:
        if day < start:
            continue
        if day > end:
            break
        for instant in sorted({local_instant(day, value, plan['timezone']) for value in plan['times']}):
            produced += 1
            if count and produced > count:
                return None
            if instant > after:
                return instant
    return None


def _decode(row):
    item = dict(row)
    item['plan'] = json.loads(item.pop('plan_json'))
    item['enabled'] = bool(item['enabled'])
    return item


def list_schedules(sub_id):
    with db.connect() as conn:
        return [_decode(row) for row in conn.execute('SELECT * FROM subscription_schedules WHERE sub_id=? ORDER BY kind,id', (sub_id,))]


def save_schedule(sub_id, source, schedule_id=None, *, now=None):
    now = utc(now)
    plan = normalize_plan(source, now=now)
    if plan['kind'] == 'interval':
        if schedule_id and schedule_id != f'interval-{sub_id}':
            raise ValueError('固定计划不能改为间隔计划；请单独编辑间隔计划')
        schedule_id = f'interval-{sub_id}'
    schedule_id = schedule_id or uuid.uuid4().hex
    due = next_occurrence(plan, now - dt.timedelta(seconds=1))
    with db.connect() as conn:
        if not conn.execute('SELECT 1 FROM subscriptions WHERE id=?', (sub_id,)).fetchone():
            raise ValueError('订阅不存在')
        owner = conn.execute('SELECT sub_id,kind FROM subscription_schedules WHERE id=?', (schedule_id,)).fetchone()
        if owner and owner['sub_id'] != sub_id:
            raise ValueError('计划不属于此订阅')
        if owner and owner['kind'] == 'interval' and plan['kind'] != 'interval':
            raise ValueError('间隔计划不能改为固定计划；请另外添加固定计划')
        conn.execute('INSERT INTO subscription_schedules(id,sub_id,kind,plan_json,enabled,next_due) VALUES (?,?,?,?,?,?) '
                     'ON CONFLICT(id) DO UPDATE SET kind=excluded.kind,plan_json=excluded.plan_json,enabled=excluded.enabled,'
                     'next_due=excluded.next_due,retry_due=NULL,retry_count=0,revision=revision+1',
                     (schedule_id, sub_id, plan['kind'], json.dumps(plan), int(plan['enabled']), stamp(due) if due else None))
        if plan['kind'] == 'interval':
            conn.execute('UPDATE subscriptions SET interval_minutes=? WHERE id=?', (plan['minutes'], sub_id))
    return next(item for item in list_schedules(sub_id) if item['id'] == schedule_id)


def delete_schedule(schedule_id):
    with db.connect() as conn:
        row = conn.execute('SELECT kind FROM subscription_schedules WHERE id=?', (schedule_id,)).fetchone()
        if row and row['kind'] == 'interval':
            raise ValueError('间隔计划请使用启用开关，不可删除')
        return bool(conn.execute('DELETE FROM subscription_schedules WHERE id=?', (schedule_id,)).rowcount)


def ensure_interval(sub, *, now=None):
    now = utc(now)
    minutes = max(5, int(sub.get('interval_minutes') or 30))
    plan = normalize_plan({'kind': 'interval', 'minutes': minutes})
    previous = sub.get('last_scan_at')
    try:
        due = utc(previous) + dt.timedelta(minutes=minutes) if previous else now
    except ValueError:
        due = now
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM subscription_schedules WHERE sub_id=? AND kind='interval'", (sub['id'],)).fetchone()
        if row is None:
            conn.execute('INSERT INTO subscription_schedules(id,sub_id,kind,plan_json,next_due) VALUES (?,?,?,?,?)',
                         (f"interval-{sub['id']}", sub['id'], 'interval', json.dumps(plan), stamp(due)))
        elif json.loads(row['plan_json'])['minutes'] != minutes:
            # A deliberate interval-setting edit resets only this interval cursor.
            plan['enabled'] = bool(row['enabled'])
            conn.execute('UPDATE subscription_schedules SET plan_json=?,next_due=?,revision=revision+1 WHERE id=?',
                         (json.dumps(plan), stamp(now + dt.timedelta(minutes=minutes)), row['id']))


def due_groups(now=None, *, limit=100):
    current = stamp(now)
    with db.connect() as conn:
        return [row['sub_id'] for row in conn.execute(
            'SELECT s.sub_id FROM subscription_schedules s JOIN subscriptions b ON b.id=s.sub_id '
            'WHERE b.enabled=1 AND s.enabled=1 AND (s.next_due<=? OR s.retry_due<=?) '
            "AND NOT EXISTS(SELECT 1 FROM schedule_runs r WHERE r.sub_id=s.sub_id AND r.status='running') "
            'GROUP BY s.sub_id ORDER BY MIN(CASE WHEN s.next_due<=? THEN s.next_due ELSE s.retry_due END),s.sub_id LIMIT ?',
            (current, current, current, max(1, min(100, limit))))]


def claim_group(sub_id, now=None):
    now = utc(now)
    current = stamp(now)
    with db.connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        if not conn.execute('SELECT 1 FROM subscriptions WHERE id=? AND enabled=1', (sub_id,)).fetchone():
            return None
        if conn.execute("SELECT 1 FROM schedule_runs WHERE sub_id=? AND status='running'", (sub_id,)).fetchone():
            return None
        rows = conn.execute('SELECT * FROM subscription_schedules WHERE sub_id=? AND enabled=1 AND (next_due<=? OR retry_due<=?)',
                            (sub_id, current, current)).fetchall()
        if not rows:
            return None
        contexts = []
        for row in rows:
            normal = bool(row['next_due'] and row['next_due'] <= current)
            plan = json.loads(row['plan_json'])
            due = row['next_due'] if normal else row['retry_due']
            following = row['next_due']
            if normal:
                if row['kind'] == 'interval':
                    step = dt.timedelta(minutes=plan['minutes'])
                    prior = utc(row['next_due'])
                    following = stamp(prior + step * (int((now - prior) // step) + 1))
                else:
                    next_time = next_occurrence(plan, now)
                    following = stamp(next_time) if next_time else None
            attempts = 0 if normal else row['retry_count']
            contexts.append({'id': row['id'], 'due': due, 'normal': normal, 'retry_count': attempts, 'revision': row['revision']})
            conn.execute('UPDATE subscription_schedules SET next_due=?,last_due=?,retry_due=NULL,retry_count=? WHERE id=?',
                         (following, due, attempts, row['id']))
        run_id = uuid.uuid4().hex
        trigger = 'scheduled' if any(item['normal'] for item in contexts) else 'retry'
        conn.execute('INSERT INTO schedule_runs(id,sub_id,schedule_ids_json,due_at,trigger,status,started_at) '
                     "VALUES (?,?,?,?,?,'running',?)", (run_id, sub_id, json.dumps(contexts), min(item['due'] for item in contexts), trigger, current))
    return {'id': run_id, 'sub_id': sub_id, 'trigger': trigger, 'schedules': contexts}


def finish_group(run_id, result, *, interrupted=False, now=None):
    now = utc(now)
    error = redact_text(str(result.get('error') or ('应用退出，等待下次启动补扫' if interrupted else '')))
    coverage = result.get('coverage') or {}
    status = 'interrupted' if interrupted else 'failed' if error else 'partial' if coverage.get('complete') is False else 'success'
    if status == 'partial':
        reasons = {'page_limit': '达到页数上限', 'item_limit': '达到条目上限',
                   'time_limit': '达到扫描时限', 'pagination_stalled': '平台未继续返回分页内容'}
        reason = str(coverage.get('reason') or 'unknown')[:100]
        error = '回溯覆盖不足：' + reasons.get(reason, redact_text(reason))
    with db.connect() as conn:
        run = conn.execute('SELECT * FROM schedule_runs WHERE id=?', (run_id,)).fetchone()
        if not run or run['status'] != 'running':
            return
        conn.execute('UPDATE schedule_runs SET status=?,finished_at=?,error=?,scan_id=? WHERE id=?',
                     (status, stamp(now), error, result.get('scan_id'), run_id))
        for context in json.loads(run['schedule_ids_json']):
            attempt = context['retry_count'] + (1 if status == 'failed' else 0)
            retry = (now if interrupted else now + dt.timedelta(seconds=30 * 2 ** (attempt - 1))
                     if status == 'failed' and attempt <= 3 else None)
            conn.execute('UPDATE subscription_schedules SET last_status=?,last_error=?,retry_count=?,retry_due=? WHERE id=? AND revision=?',
                         (status, error, attempt, stamp(retry) if retry else None, context['id'], context['revision']))


def recover_runs(now=None):
    while True:
        with db.connect() as conn:
            runs = conn.execute("SELECT id FROM schedule_runs WHERE status='running' LIMIT 100").fetchall()
        if not runs:
            return
        for run in runs:
            finish_group(run['id'], {}, interrupted=True, now=now)


def list_runs(sub_id, *, limit=50, offset=0):
    from .subscription_store import public_coverage
    with db.connect() as conn:
        rows = conn.execute('SELECT r.*,substr(s.snapshot_json,1,16385) AS scan_snapshot '
                            'FROM schedule_runs r LEFT JOIN subscription_scans s ON s.id=r.scan_id '
                            'WHERE r.sub_id=? ORDER BY r.started_at DESC,r.id DESC LIMIT ? OFFSET ?',
                            (sub_id, max(1, min(100, int(limit))), max(0, int(offset)))).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item['schedules'] = json.loads(item.pop('schedule_ids_json'))
        item['coverage'] = public_coverage(item.pop('scan_snapshot'))
        result.append(item)
    return result


DEFAULT_LIMITS = {'max_pages': 5, 'max_items': 200, 'timeout_seconds': 60, 'overlap_hours': 48}


def scan_options(sub_id):
    with db.connect() as conn:
        row = conn.execute('SELECT limits_json FROM subscription_scan_options WHERE sub_id=?', (sub_id,)).fetchone()
        coverage = conn.execute('SELECT * FROM subscription_coverage WHERE sub_id=?', (sub_id,)).fetchone()
        baseline = conn.execute('SELECT 1 FROM subscription_state t JOIN subscriptions s '
                                'ON s.platform=t.platform AND s.blogger_id=t.blogger_id WHERE s.id=?', (sub_id,)).fetchone()
    options = {**DEFAULT_LIMITS, **(json.loads(row['limits_json']) if row else {})}
    last = coverage['covered_until'] if coverage else None
    options['cutoff_at'] = stamp(utc(last) - dt.timedelta(hours=options['overlap_hours'])) if last else None
    options['baseline'] = baseline is None
    return options


def save_scan_options(sub_id, source):
    ranges = {'max_pages': (1, 20), 'max_items': (1, 2000), 'timeout_seconds': (10, 180), 'overlap_hours': (1, 720)}
    limits = {key: _integer(source.get(key, value), key, *ranges[key]) for key, value in DEFAULT_LIMITS.items()}
    with db.connect() as conn:
        conn.execute('INSERT INTO subscription_scan_options VALUES (?,?) ON CONFLICT(sub_id) DO UPDATE SET limits_json=excluded.limits_json',
                     (sub_id, json.dumps(limits)))
    return limits


def record_coverage(sub_id, coverage, *, now=None):
    timestamp = stamp(now)
    complete = bool(coverage.get('complete'))
    with db.connect() as conn:
        conn.execute('INSERT INTO subscription_coverage(sub_id,last_success_at,covered_until,coverage_json) VALUES (?,?,?,?) '
                     'ON CONFLICT(sub_id) DO UPDATE SET last_success_at=excluded.last_success_at,'
                     'covered_until=COALESCE(excluded.covered_until,subscription_coverage.covered_until),coverage_json=excluded.coverage_json',
                     (sub_id, timestamp, timestamp if complete else None, json.dumps(coverage)))
