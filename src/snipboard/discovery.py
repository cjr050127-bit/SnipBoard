"""Bounded, read-only local Snipaste discovery. No drive-wide recursive search."""
from pathlib import Path
import ctypes
from ctypes import wintypes
import json
import os
import re
import subprocess
import sys

from .source import GROUP_ID, history_directory, normalize_source, store_locations


def running_executables():
    if os.name != 'nt':
        return []
    class Entry(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('usage', wintypes.DWORD), ('pid', wintypes.DWORD),
                    ('heap', ctypes.c_size_t), ('module', wintypes.DWORD), ('threads', wintypes.DWORD),
                    ('parent', wintypes.DWORD), ('priority', wintypes.LONG), ('flags', wintypes.DWORD),
                    ('name', wintypes.WCHAR * 260)]
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    for name in ('Process32FirstW', 'Process32NextW'):
        getattr(kernel, name).argtypes = (wintypes.HANDLE, ctypes.POINTER(Entry))
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        return []
    paths = []
    try:
        entry = Entry()
        entry.size = ctypes.sizeof(entry)
        valid = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while valid:
            if entry.name.casefold() == 'snipaste.exe':
                handle = kernel.OpenProcess(0x1000, False, entry.pid)
                if handle:
                    try:
                        buffer, size = ctypes.create_unicode_buffer(32768), wintypes.DWORD(32768)
                        if kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                            paths.append(Path(buffer.value))
                    finally:
                        kernel.CloseHandle(handle)
            valid = kernel.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(snapshot)
    return paths


def registry_locations():
    if os.name != 'nt':
        return []
    import winreg
    result = []
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            for branch in (r'SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\Snipaste.exe',
                           r'SOFTWARE\Microsoft\Windows\CurrentVersion\Run'):
                try:
                    with winreg.OpenKey(hive, branch, 0, winreg.KEY_READ | view) as key:
                        for i in range(winreg.QueryInfoKey(key)[1]):
                            name, value, _ = winreg.EnumValue(key, i)
                            if not isinstance(value, str):
                                continue
                            match = re.match(r'^\s*"?(.+?snipaste\.exe)(?:"|\s|$)', value, re.I)
                            if match:
                                result.append(Path(os.path.expandvars(match[1])))
                except OSError:
                    pass
            try:
                with winreg.OpenKey(hive, r'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall', 0, winreg.KEY_READ | view) as root:
                    for i in range(min(winreg.QueryInfoKey(root)[0], 2000)):
                        try:
                            with winreg.OpenKey(root, winreg.EnumKey(root, i)) as key:
                                name = winreg.QueryValueEx(key, 'DisplayName')[0]
                                if isinstance(name, str) and 'snipaste' in name.casefold():
                                    value = winreg.QueryValueEx(key, 'InstallLocation')[0]
                                    if isinstance(value, str) and value.strip():
                                        result.append(Path(os.path.expandvars(value.strip('"'))))
                        except OSError:
                            pass
            except OSError:
                pass
    return result


def shortcut_locations():
    if os.name != 'nt':
        return []
    roots = [Path.home() / 'Desktop', Path(os.environ.get('PUBLIC', 'C:/Users/Public')) / 'Desktop']
    for key in ('APPDATA', 'PROGRAMDATA'):
        if os.environ.get(key):
            roots.append(Path(os.environ[key]) / 'Microsoft/Windows/Start Menu/Programs')
    if os.environ.get('OneDrive'):
        roots.append(Path(os.environ['OneDrive']) / 'Desktop')
    links = []
    for root in roots:
        for index, (folder, dirs, files) in enumerate(os.walk(root, followlinks=False)):
            if index >= 500:
                break
            if len(Path(folder).relative_to(root).parts) >= 4:
                dirs[:] = []
            links.extend(str(Path(folder) / f) for f in files if 'snipaste' in f.casefold() and f.lower().endswith('.lnk'))
            if len(links) >= 100:
                break
    if not links:
        return []
    # Fixed script; shortcut paths are JSON data, never executable shell text.
    script = "$ErrorActionPreference='Stop'; $OutputEncoding=[Console]::OutputEncoding=[Text.UTF8Encoding]::new(); $w=New-Object -ComObject WScript.Shell; $p=ConvertFrom-Json ([Console]::In.ReadToEnd()); @($p | ForEach-Object { $w.CreateShortcut($_).TargetPath }) | ConvertTo-Json -Compress"
    try:
        process = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
            input=json.dumps(links[:100]), capture_output=True, encoding='utf-8', timeout=6,
            creationflags=subprocess.CREATE_NO_WINDOW)
        values = json.loads(process.stdout.lstrip('\ufeff')) if process.returncode == 0 else []
        if isinstance(values, str):
            values = [values]
        return [Path(v) for v in values if isinstance(v, str) and Path(v).name.casefold() == 'snipaste.exe']
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return []


def common_locations():
    bases = [Path.home() / name for name in ('Desktop', 'Downloads', 'Documents', 'scoop/apps/snipaste')]
    bases += [Path(sys.executable).parent, Path.cwd()]
    for key in ('LOCALAPPDATA', 'APPDATA', 'ProgramFiles', 'ProgramFiles(x86)'):
        if os.environ.get(key):
            base = Path(os.environ[key])
            bases += [base, base / 'Programs', base / 'snipaste.com']
    if os.name == 'nt':
        kernel = ctypes.WinDLL('kernel32')
        kernel.GetDriveTypeW.argtypes = (wintypes.LPCWSTR,)
        for drive in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ':
            root = Path(f'{drive}:/')
            if kernel.GetDriveTypeW(str(root)) == 3:  # Fixed disks only.
                bases += [root / name for name in ('', 'Tools', 'Apps', 'Software', 'PortableApps')]
    found = []
    for base in dict.fromkeys(bases):
        found.append(base / 'Snipaste')
        try:
            with os.scandir(base) as entries:
                for i, entry in enumerate(entries):
                    if i >= 1000:
                        break
                    if 'snipaste' in entry.name.casefold() and entry.is_dir(follow_symlinks=False):
                        folder = Path(entry.path)
                        found += [folder, folder / 'current']
        except OSError:
            pass
    return found


def discover_candidates():
    candidates = {}
    providers = [(running_executables, '正在运行'), (store_locations, '微软商店版'),
                 (registry_locations, '安装或启动记录'), (shortcut_locations, '快捷方式'),
                 (common_locations, '常见位置')]
    for provider, reason in providers:
        try:
            paths = provider()
        except OSError:
            continue
        for path in paths:
            try:
                source = normalize_source(path)
                history = history_directory(source)
                key = os.path.normcase(str(history))
                if key in candidates:
                    continue
                groups = sum(1 for p in history.iterdir() if GROUP_ID.fullmatch(p.name) and p.is_dir() and not p.is_symlink())
                candidates[key] = dict(path=str(source), history=str(history), reason=reason, groups=groups)
            except (OSError, ValueError):
                continue
    return list(candidates.values())
