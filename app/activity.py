"""Small shared snapshot for the tray and visible browser pages. No storage or network access."""

from . import live_service, task_manager, watcher


def counts():
    tasks = tuple(task_manager.TASKS.values())
    return {
        'running': sum(t.get('status') == 'running' for t in tasks),
        'queued': sum(t.get('status') == 'queued' for t in tasks),
        'scanning': len(watcher._SCANNING),
        'recording': live_service.active_count(),
    }
