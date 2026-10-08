"""Check tracked paths and obvious credential literals without printing values."""
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
BLOCKED_PATH = re.compile(
    r'(?i)(?:^|/)(?:app_data|release|backend-dist|node_modules|playwright-report|test-results)/'
    r'|(?:^|/)\.env(?:\..+)?$'
    r'|\.(?:pem|key|pfx|p12|keystore|har|db|sqlite3?|log|dmp)$'
    r'|(?:cookie|storage-state|storageState|session)[^/]*\.json$'
    r'|(?:report|audit)[^/]*\.md$'
    r'|(?:^|/)(?:RELEASE_[^/]*|CURRENT_BASELINE)\.md$'
)
PATTERNS = {
    'private-key': re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----'),
    'github-token': re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b'),
    'aws-access-key': re.compile(r'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b'),
    'google-api-key': re.compile(r'\bAIza[A-Za-z0-9_-]{35}\b'),
    'bearer-literal': re.compile(r'(?i)Bearer\s+[A-Za-z0-9_.-]{32,}'),
    'cookie-literal': re.compile(r'(?i)\b(?:SUB|SUBP|sessionid|sessionid_ss|ttwid|SESSDATA)=[A-Za-z0-9_%.-]{20,}'),
    'credential-literal': re.compile(r'''(?i)["']?(?:api[_-]?key|access[_-]?token|client[_-]?secret|password)["']?\s*[:=]\s*["'][A-Za-z0-9_+/=.-]{16,}["']'''),
    'personal-home-path': re.compile(r'(?:[A-Za-z]:[/\\](?:Users|Documents and Settings)[/\\][^\s/\\"\']+|/(?:Users|home)/[a-zA-Z0-9_.-]+/)'),
}


def findings(text):
    return [(number, kind) for number, line in enumerate(text.splitlines(), 1)
            for kind, pattern in PATTERNS.items() if pattern.search(line)]


def main():
    paths = subprocess.check_output(['git', 'ls-files', '-z'], cwd=ROOT).decode().split('\0')
    problems = []
    for name in filter(None, paths):
        if BLOCKED_PATH.search(name) and not name.endswith(('.env.example', '.env.sample')):
            problems.append(f'{name}: blocked tracked path')
        path = ROOT / name
        if not path.is_file():
            continue
        raw = path.read_bytes()
        if b'\0' in raw:
            continue
        for number, kind in findings(raw.decode('utf-8', errors='replace')):
            problems.append(f'{name}:{number}: {kind}')
    for problem in problems:
        print(problem)
    print(f'Repository privacy check: {len(problems)} finding(s); credential values are never printed.')
    return int(bool(problems))


if __name__ == '__main__':
    sys.exit(main())
