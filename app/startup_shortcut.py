"""Manage one current-user Startup shortcut with Windows Shell COM.

Paths and arguments are JSON data on stdin, never PowerShell source. Reading
does not save a shortcut. Updates are atomic and support byte-exact rollback.
"""
import base64
import json
import os
from pathlib import Path
import subprocess

from .redaction import redact_text


_SCRIPT = r'''
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::InputEncoding = [Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$request = [Console]::In.ReadToEnd() | ConvertFrom-Json
$folder = [Environment]::GetFolderPath('Startup')
if (-not $folder) { throw 'Windows Startup folder is unavailable' }
$path = Join-Path $folder 'Media Squirrel.lnk'
$temporary = Join-Path $folder ('MediaSquirrel-' + [Guid]::NewGuid().ToString('N') + '.tmp')
$backup = $temporary + '.bak'
$draft = Join-Path ([IO.Path]::GetTempPath()) ('MediaSquirrel-' + [Guid]::NewGuid().ToString('N') + '.lnk')
try {
    if ($request.action -eq 'write') {
        $shell = New-Object -ComObject WScript.Shell
        $link = $shell.CreateShortcut($draft)
        $link.TargetPath = $request.value.target
        $link.Arguments = $request.value.arguments
        $link.WorkingDirectory = $request.value.working_directory
        $link.Description = 'Media Squirrel - start in the background at sign-in'
        $link.Save()
        $content = [IO.File]::ReadAllBytes($draft)
    } elseif ($request.action -eq 'restore' -and $request.content) {
        $content = [Convert]::FromBase64String($request.content)
    } elseif ($request.action -notin @('read', 'remove', 'restore')) {
        throw 'Unsupported shortcut operation'
    }
    if ($null -ne $content) {
        [IO.Directory]::CreateDirectory($folder) | Out-Null
        [IO.File]::WriteAllBytes($temporary, $content)
        if ([IO.File]::Exists($path)) { [IO.File]::Replace($temporary, $path, $backup) }
        else { [IO.File]::Move($temporary, $path) }
    } elseif ($request.action -in @('remove', 'restore')) {
        [IO.File]::Delete($path)
    }
    if (-not [IO.File]::Exists($path)) { 'null'; exit 0 }
    if ((Get-Item -LiteralPath $path).Length -gt 262144) { throw 'Unexpected startup shortcut size' }
    $shell = New-Object -ComObject WScript.Shell
    $link = $shell.CreateShortcut($path)
    @{ target=$link.TargetPath; arguments=$link.Arguments; working_directory=$link.WorkingDirectory;
       content=[Convert]::ToBase64String([IO.File]::ReadAllBytes($path)) } | ConvertTo-Json -Compress
} catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
} finally {
    [IO.File]::Delete($temporary)
    [IO.File]::Delete($backup)
    [IO.File]::Delete($draft)
}
'''


def shortcut(action='read', *, value=None, content=None):
    if action not in {'read', 'write', 'remove', 'restore'}:
        raise ValueError('不支持的自启动操作')
    executable = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    command = base64.b64encode(_SCRIPT.encode('utf-16-le')).decode('ascii')
    try:
        result = subprocess.run(
            [str(executable), '-NoLogo', '-NoProfile', '-NonInteractive', '-EncodedCommand', command],
            input=json.dumps({'action': action, 'value': value, 'content': content}, ensure_ascii=False),
            encoding='utf-8', errors='replace', stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), timeout=15, check=True)
        response = json.loads(result.stdout.lstrip('\ufeff').strip())
        if response is not None and (not isinstance(response, dict) or
                any(not isinstance(response.get(key), str) for key in
                    ('target', 'arguments', 'working_directory', 'content'))):
            raise ValueError('无效的快捷方式状态')
        return response
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        details = error.stderr if isinstance(error, subprocess.CalledProcessError) else str(error)
        raise RuntimeError('无法读写 Windows 启动快捷方式：' + redact_text(str(details))[:800]) from error


def matches(actual, expected):
    if not actual:
        return False
    return (os.path.normcase(os.path.normpath(actual['target'])) ==
            os.path.normcase(os.path.normpath(expected['target'])) and
            actual['arguments'] == expected['arguments'] and
            os.path.normcase(os.path.normpath(actual['working_directory'])) ==
            os.path.normcase(os.path.normpath(expected['working_directory'])))
