"""Read-only observations. Undocumented formats never establish live state."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import hashlib
import os
import re
import struct
import configparser

GROUP_ID = re.compile(r"[A-Z0-9]{6}\Z")
GROUP_LOG = re.compile(r'^\[(?P<time>[^\]]+)\] \[I\] Group (?P<index>\d+) \[(?P<id>[A-Z0-9]{6})\]: "(?P<name>.*)"$')


def discover() -> list[Path]:
    from .discovery import discover_candidates
    return [Path(candidate['path']) for candidate in discover_candidates()]


def store_locations() -> list[Path]:
    """Return all candidates; never silently pick between installations."""
    roots = [Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "Packages"]
    for drive in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        if os.name == 'nt':
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.WinDLL('kernel32')
            kernel.GetDriveTypeW.argtypes = (wintypes.LPCWSTR,)
            if kernel.GetDriveTypeW(f'{drive}:\\') != 3:
                continue
        wp = Path(f"{drive}:/WpSystem")
        try:
            roots.extend(p / "AppData/Local/Packages" for p in wp.iterdir() if p.is_dir())
        except OSError:
            continue
    results = set()
    for root in roots:
        try:
            for package in root.glob("45479liulios.17062D84F7C46*"):
                for state in (package / 'LocalState', package / 'LocalCache/Local/snipaste.com/Snipaste'):
                    if (state / 'history').is_dir() or (state / 'config.ini').is_file():
                        results.add(state.resolve())
        except OSError:
            continue
    return sorted(results)


def history_directory(state: Path) -> Path:
    """Resolve default or configured storage without changing Snipaste files."""
    state = Path(state).resolve()
    config = state / 'config.ini'
    if config.is_file():
        parser = configparser.ConfigParser(interpolation=None, strict=False)
        try:
            parser.read_string(stable_read(config, 1024 * 1024).decode('utf-8-sig'))
            value = parser.get('General', 'history_dir', fallback='').strip().strip('"')
        except (ValueError, UnicodeError, configparser.Error):
            value = ''
        if value:
            path = Path(os.path.expandvars(value.replace('\\\\', '\\'))).expanduser()
            path = path if path.is_absolute() else state / path
            if path.is_dir():
                return path.resolve()
    if (state / 'history').is_dir():
        return (state / 'history').resolve()
    if state.is_dir() and (state.name.casefold() == 'history' or (state / '.sp0').is_file()):
        return state
    if state.is_dir():
        for child in state.iterdir():
            if GROUP_ID.fullmatch(child.name) and child.is_dir() and (child / '.sp2').is_file():
                return state
    raise ValueError('未找到贴图数据。请先在 Snipaste 中保存贴图，再选择程序或数据所在文件夹。')


def normalize_source(path: Path) -> Path:
    path = Path(path).expanduser().resolve()
    if path.is_file() and path.name.casefold() in ('snipaste.exe', 'config.ini'):
        path = path.parent
    if path.name.casefold() == 'history':
        try:
            if history_directory(path.parent) == path:
                path = path.parent
        except (OSError, ValueError):
            pass
    history = history_directory(path)
    # Probe access now; never select an unreadable location and mark old files absent.
    with os.scandir(history) as entries:
        next(entries, None)
    return path


def stable_read(path: Path, limit: int = 32 * 1024 * 1024) -> bytes:
    before = path.stat()
    if before.st_size > limit:
        raise ValueError(f"File exceeds observation limit: {path}")
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or len(data) != after.st_size:
        raise ValueError(f"File changed while reading; retry: {path}")
    return data


def qstring(data: bytes, offset: int) -> tuple[str | None, int]:
    if offset + 4 > len(data):
        raise ValueError("Truncated QString length")
    size = struct.unpack_from(">I", data, offset)[0]
    if size == 0xFFFFFFFF:
        return None, offset + 4
    if size % 2 or size > 8192 or offset + 4 + size > len(data):
        raise ValueError("Invalid QString size")
    return data[offset + 4:offset + 4 + size].decode("utf-16-be"), offset + 4 + size


def restore_observations(data: bytes) -> dict:
    magic, offset = qstring(data, 0)
    if magic != "SNIPASTE-RESOTRE":
        raise ValueError("Unrecognized restore header")
    if len(data) < offset + 4:
        raise ValueError("Truncated restore version")
    version = struct.unpack_from(">I", data, offset)[0]
    if version != 201:
        raise ValueError(f"Unsupported restore version: {version}")
    # Evidence extraction only: no assumptions about the undocumented preceding layout.
    groups = {}
    trailing_id = None
    pattern = rb"\x00\x00\x00\x0c(?:\x00[A-Z0-9]){6}"
    for match in re.finditer(pattern, data):
        group_id, end = qstring(data, match.start())
        if end == len(data):
            trailing_id = group_id
            continue
        try:
            name, _ = qstring(data, end)
            if name is None or all(c.isprintable() for c in name):
                groups[group_id] = name
        except (ValueError, UnicodeError):
            continue
    return {"version": version, "group_name_candidates": groups,
            "trailing_group_id_observation": trailing_id,
            "sha256": hashlib.sha256(data).hexdigest()}


@dataclass(frozen=True)
class Group:
    id: str
    name_candidate: str | None
    image_files: tuple[str, ...]
    metadata_present: bool


def inspect_source(state: Path) -> dict:
    state = state.resolve()
    history = history_directory(state)
    warnings = []
    restore = {}
    try:
        restore = restore_observations(stable_read(history / ".sp0"))
    except (OSError, ValueError, UnicodeError) as exc:
        warnings.append(str(exc))
    groups = []
    for directory in sorted(history.iterdir()):
        if not GROUP_ID.fullmatch(directory.name) or not directory.is_dir() or directory.is_symlink():
            continue
        if not directory.resolve().is_relative_to(history.resolve()):
            warnings.append(f"Skipped external directory: {directory.name}")
            continue
        images = tuple(p.name for p in sorted(directory.glob("*.png")) if p.is_file() and not p.is_symlink())
        groups.append(asdict(Group(directory.name, restore.get("group_name_candidates", {}).get(directory.name),
                                   images, (directory / ".sp2").is_file())))
    last_log_group = None
    try:
        for line in stable_read(state / "splog.txt").decode("utf-8-sig", errors="replace").splitlines():
            match = GROUP_LOG.fullmatch(line)
            if match:
                last_log_group = match.groupdict()
    except (OSError, ValueError) as exc:
        warnings.append(str(exc))
    return {"source": str(state), "groups": groups, "restore": restore,
            "last_log_group_observation": last_log_group,
            "current_group": {"status": "unverified", "id": None,
                              "reason": "Restore files and historical logs do not establish the live active group."},
            "membership_status": "file_inventory_only", "warnings": warnings}
