"""Calendar checks and interval checks retain independent durable cursors."""
from datetime import timedelta
from . import scheduling

SCHEMA = """
ALTER TABLE live_monitors ADD COLUMN source TEXT NOT NULL DEFAULT 'room';
ALTER TABLE live_monitors ADD COLUMN quality TEXT NOT NULL DEFAULT 'best';
ALTER TABLE live_recordings ADD COLUMN quality TEXT NOT NULL DEFAULT '';
ALTER TABLE live_monitors ADD COLUMN interval_enabled INTEGER NOT NULL DEFAULT 1;
ALTER TABLE live_monitors ADD COLUMN plan_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE live_monitors ADD COLUMN next_interval_at TEXT NOT NULL DEFAULT '';
ALTER TABLE live_monitors ADD COLUMN next_plan_at TEXT NOT NULL DEFAULT '';
UPDATE live_monitors SET next_interval_at=next_check_at;
UPDATE live_monitors SET next_check_at=strftime('%Y-%m-%dT%H:%M:%f+00:00','now') WHERE next_check_at='';
"""


def cursors(monitor, *, now=None, initial=False, active=False):
    now = scheduling.utc(now)
    stamp = now.isoformat(timespec='milliseconds')
    interval = monitor.get('next_interval_at') or stamp
    plan_due = monitor.get('next_plan_at') or ''
    plan = monitor.get('plan')
    if monitor.get('interval_enabled', True):
        if initial:
            interval = stamp
        elif interval <= stamp:
            interval = (now + timedelta(seconds=monitor['interval_seconds'])).isoformat(timespec='milliseconds')
    else:
        interval = ''
    if plan:
        if initial or (plan_due and plan_due <= stamp):
            after = now-timedelta(seconds=1) if initial else now
            value = scheduling.next_occurrence(plan, after)
            plan_due = value.isoformat(timespec='milliseconds') if value else ''
    else:
        plan_due = ''
    candidates = [value for value in (interval, plan_due) if value]
    if active:
        candidates.append((now+timedelta(seconds=60)).isoformat(timespec='milliseconds'))
    return dict(next_interval_at=interval, next_plan_at=plan_due, next_check_at=min(candidates, default=''))
