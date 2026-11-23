"""Versioned, read-only HTTP representations. Inputs are snapshots from the single task owner."""

PROTOCOL = 1
ROUTES = {'/api/home': 'home', '/api/scripts': 'scripts', '/api/activity': 'activity'}


def render(path, data):
    source = ROUTES[path]
    if source == 'home':
        result = data['summary']
        result['live'].update(active=data['active'], current=data['monitor'])
        result['scans'].update(active=data['scanning'], progress=data['progress'])
        result['updated_at'] = data['updated_at']
        return result
    if source == 'scripts':
        return data['scripts']
    return {**data['counts'], 'recordings': data['recordings']}
