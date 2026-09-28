'''Windows integrations using explicit pointer-sized handles.'''
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import os


def snipaste_session():
    if os.name != 'nt':
        return None
    class Entry(ctypes.Structure):
        _fields_ = [('dwSize', wintypes.DWORD), ('cntUsage', wintypes.DWORD),
                    ('th32ProcessID', wintypes.DWORD), ('th32DefaultHeapID', ctypes.c_size_t),
                    ('th32ModuleID', wintypes.DWORD), ('cntThreads', wintypes.DWORD),
                    ('th32ParentProcessID', wintypes.DWORD), ('pcPriClassBase', wintypes.LONG),
                    ('dwFlags', wintypes.DWORD), ('szExeFile', wintypes.WCHAR * 260)]
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = (wintypes.HANDLE, ctypes.POINTER(Entry))
    kernel.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.POINTER(Entry))
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = (wintypes.HANDLE, *([ctypes.POINTER(wintypes.FILETIME)] * 4))
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    pids = []
    try:
        entry = Entry()
        entry.dwSize = ctypes.sizeof(entry)
        valid = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while valid:
            if entry.szExeFile.lower() == 'snipaste.exe':
                pids.append(entry.th32ProcessID)
            valid = kernel.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(snapshot)
    if len(pids) != 1:
        return None
    handle = kernel.OpenProcess(0x1000, False, pids[0])
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        created, exited, system, user = [wintypes.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(handle, *map(ctypes.byref, (created, exited, system, user))):
            raise ctypes.WinError(ctypes.get_last_error())
        seconds = ((created.dwHighDateTime << 32) | created.dwLowDateTime) / 10000000 - 11644473600
        started = datetime.fromtimestamp(seconds, timezone.utc).astimezone().replace(tzinfo=None)
        return pids[0], started
    finally:
        kernel.CloseHandle(handle)


class DesktopIntegration:
    def __init__(self, root, on_hotkey):
        self.root, self.on_hotkey = root, on_hotkey
        self.mutex, self.hotkey, self.after_id = None, False, None
        if os.name != 'nt':
            self.status = '全局快捷键仅支持 Windows'
            return
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.kernel.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
        self.kernel.CreateMutexW.restype = wintypes.HANDLE
        self.kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        self.mutex = self.kernel.CreateMutexW(None, False, 'Local\\SnipBoardDesktop')
        if not self.mutex:
            raise ctypes.WinError(ctypes.get_last_error())
        if ctypes.get_last_error() == 183:
            self.kernel.CloseHandle(self.mutex)
            self.mutex = None
            raise ValueError('SnipBoard 已运行，请使用现有窗口。')
        self.user = ctypes.WinDLL('user32', use_last_error=True)
        self.user.RegisterHotKey.argtypes = (wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT)
        self.user.UnregisterHotKey.argtypes = (wintypes.HWND, ctypes.c_int)
        self.user.PeekMessageW.argtypes = (ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                                          wintypes.UINT, wintypes.UINT, wintypes.UINT)
        self.hotkey = bool(self.user.RegisterHotKey(None, 0x5342, 0x4000 | 2 | 1, ord('B')))
        self.status = 'Ctrl+Alt+B：打开当前图组' if self.hotkey else 'Ctrl+Alt+B 已被占用'
        self._poll()

    def _poll(self):
        if self.hotkey:
            message = wintypes.MSG()
            while self.user.PeekMessageW(ctypes.byref(message), None, 0x0312, 0x0312, 1):
                if message.wParam == 0x5342:
                    self.on_hotkey()
        self.after_id = self.root.after(80, self._poll)

    def close(self):
        if self.after_id:
            self.root.after_cancel(self.after_id)
        if self.hotkey:
            self.user.UnregisterHotKey(None, 0x5342)
        if self.mutex:
            self.kernel.CloseHandle(self.mutex)
            self.mutex = None
