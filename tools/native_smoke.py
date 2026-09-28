"""Test our own Windows hotkey message and hidden-window Z order; no user input injection."""
import ctypes
from ctypes import wintypes
import json
import os
os.environ['QT_QPA_PLATFORM'] = 'windows'
from PySide6.QtWidgets import QApplication, QWidget
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from snipboard.qt_windows import NativeHotkey, WindowLayer

app = QApplication([])
events = []
hotkey = NativeHotkey(app, lambda: events.append('called'))
user = ctypes.WinDLL('user32', use_last_error=True)
kernel = ctypes.WinDLL('kernel32', use_last_error=True)
kernel.GetCurrentThreadId.restype = wintypes.DWORD
user.PostThreadMessageW.argtypes = (wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
user.GetWindowLongPtrW.argtypes = (wintypes.HWND, ctypes.c_int)
user.GetWindowLongPtrW.restype = ctypes.c_ssize_t
widget = QWidget()
widget.setAttribute(Qt.WA_DontShowOnScreen)
widget.show()
app.processEvents()
layer = WindowLayer(widget)
try:
    assert user.PostThreadMessageW(kernel.GetCurrentThreadId(), 0x0312, 0x5343, 0)
    QTest.qWait(100)
    assert events == ['called'], events
    layer.set_mode('top')
    assert user.GetWindowLongPtrW(int(widget.winId()), -20) & 8
    layer.set_mode('normal')
    assert not user.GetWindowLongPtrW(int(widget.winId()), -20) & 8
    layer.set_mode('bottom')
    layer.set_mode('normal')
    print(json.dumps({'native_message_dispatch': True, 'hotkey_registered': hotkey.registered,
                      'hidden_window_topmost_toggle': True, 'bottom_api_called': True}))
finally:
    layer.timer.stop()
    hotkey.close()
    widget.close()
