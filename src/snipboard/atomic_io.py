"""Atomic publication of same-directory files, including Windows redirection."""
from __future__ import annotations

import errno
import os
from pathlib import Path


def _physical_file_path(path: Path) -> Path:
    """Resolve a file handle, since an AppData directory can hide redirected files."""
    import ctypes
    from ctypes import wintypes
    import msvcrt

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    final_path = kernel.GetFinalPathNameByHandleW
    final_path.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    final_path.restype = wintypes.DWORD
    with Path(path).open('rb') as stream:
        handle = msvcrt.get_osfhandle(stream.fileno())
        required = final_path(handle, None, 0, 0)
        if not required:
            raise ctypes.WinError(ctypes.get_last_error())
        buffer = ctypes.create_unicode_buffer(required + 1)
        length = final_path(handle, buffer, len(buffer), 0)
        if not length:
            raise ctypes.WinError(ctypes.get_last_error())
        if length >= len(buffer):
            raise OSError(errno.ENAMETOOLONG, '文件的真实路径在读取期间发生变化', str(path))
        return Path(buffer.value)


def replace_local_file(source, destination) -> None:
    """Publish a completed local file; never fall back to a partial target copy."""
    source, destination = Path(source), Path(destination)
    try:
        os.replace(source, destination)
        return
    except OSError as error:
        cross_volume = error.errno == errno.EXDEV or getattr(error, 'winerror', None) == 17
        same_parent = os.path.normcase(os.path.abspath(source.parent)) == os.path.normcase(os.path.abspath(destination.parent))
        if os.name != 'nt' or not cross_volume or not same_parent:
            raise
        physical_source = _physical_file_path(source)
        physical_target = physical_source.with_name(destination.name)
        # Preserve an existing target if it belongs to a different physical store.
        if destination.exists():
            existing_target = _physical_file_path(destination)
            if os.path.normcase(str(existing_target)) != os.path.normcase(str(physical_target)):
                raise error
        elif physical_target.exists():
            raise error
        os.replace(physical_source, physical_target)
        if not os.path.samefile(destination, physical_target):
            raise OSError(errno.EXDEV, '保存后的文件无法通过图库路径访问', str(destination))
