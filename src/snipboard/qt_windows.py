"""Windows hotkey and window-level integration for the Qt event loop."""
import ctypes
from ctypes import wintypes
import os

from PySide6.QtCore import QAbstractNativeEventFilter, QObject, QTimer, Signal, QEvent


class NativeHotkey(QAbstractNativeEventFilter):
    def __init__(self, app, callback):
        super().__init__()
        self.app, self.callback, self.registered = app, callback, False
        if os.name == 'nt':
            self.user = ctypes.WinDLL('user32', use_last_error=True)
            self.user.RegisterHotKey.argtypes = (wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT)
            self.user.UnregisterHotKey.argtypes = (wintypes.HWND, ctypes.c_int)
            self.registered = bool(self.user.RegisterHotKey(None, 0x5343, 0x4000 | 2 | 1, ord('B')))
            app.installNativeEventFilter(self)

    def nativeEventFilter(self, event_type, message):
        if os.name == 'nt':
            msg = wintypes.MSG.from_address(int(message))
            if msg.message == 0x0312 and msg.wParam == 0x5343:
                QTimer.singleShot(0, self.callback)
                return True, 0
        return False, 0

    def close(self):
        self.app.removeNativeEventFilter(self)
        if self.registered:
            self.user.UnregisterHotKey(None, 0x5343)
            self.registered = False


class WindowLayer(QObject):
    changed = Signal()

    def __init__(self, window):
        super().__init__(window)
        self.window, self.mode, self.target = window, 'normal', 0
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        window.installEventFilter(self)
        if os.name == 'nt':
            self.user = ctypes.WinDLL('user32', use_last_error=True)
            self.user.SetWindowPos.argtypes = (wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                               ctypes.c_int, ctypes.c_int, wintypes.UINT)
            self.user.SetWindowPos.restype = wintypes.BOOL
            self.get_window_style = getattr(self.user, 'GetWindowLongPtrW', self.user.GetWindowLongW)
            self.get_window_style.argtypes = (wintypes.HWND, ctypes.c_int)
            self.get_window_style.restype = ctypes.c_ssize_t
            self.user.GetForegroundWindow.restype = wintypes.HWND
            self.user.IsWindow.argtypes = (wintypes.HWND,)
            self.user.IsIconic.argtypes = (wintypes.HWND,)
            self.user.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
            self.user.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
            self.user.IsWindowVisible.argtypes = (wintypes.HWND,)

    def set_mode(self, mode, target=0):
        self.mode, self.target = mode, target
        self.timer.start(300) if mode in ('top', 'bottom', 'application') else self.timer.stop()
        self.refresh()
        self.changed.emit()

    def eventFilter(self, watched, event):
        if watched is self.window and event.type() in (QEvent.Show, QEvent.WinIdChange, QEvent.WindowStateChange):
            # Qt may recreate/restack the native surface when shown or restored.
            QTimer.singleShot(0, self.refresh)
        return super().eventFilter(watched, event)

    def _pid(self, handle):
        pid = wintypes.DWORD()
        self.user.GetWindowThreadProcessId(handle, ctypes.byref(pid))
        return pid.value

    def refresh(self):
        if os.name != 'nt' or not self.window.isVisible() or self.window.isMinimized():
            return
        handle = int(self.window.winId())
        foreground = self.user.GetForegroundWindow()
        level = -2  # HWND_NOTOPMOST
        if self.mode == 'top':
            # Repair a lost TOPMOST bit without repeatedly raising an already
            # topmost viewer above other floating UI or activating it.
            if self.get_window_style(handle, -20) & 0x00000008:
                return
            level = -1
        elif self.mode == 'bottom' and foreground != handle:
            level = 1
        elif self.mode == 'application':
            if not self.user.IsWindow(self.target):
                self.mode, self.target = 'normal', 0
                self.timer.stop()
            elif not self.user.IsIconic(self.target) and (foreground == handle or self._pid(foreground) == self._pid(self.target)):
                level = -1
        self.user.SetWindowPos(handle, level, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)

    def windows(self):
        if os.name != 'nt':
            return []
        found = []
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def visit(handle, _):
            if self.user.IsWindowVisible(handle) and self._pid(handle) != os.getpid():
                title = ctypes.create_unicode_buffer(512)
                self.user.GetWindowTextW(handle, title, 512)
                if title.value:
                    found.append((int(handle), title.value))
            return True
        callback = callback_type(visit)
        self.user.EnumWindows.argtypes = (callback_type, wintypes.LPARAM)
        self.user.EnumWindows(callback, 0)
        return found
