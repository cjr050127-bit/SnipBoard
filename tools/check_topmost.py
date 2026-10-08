import ctypes,json,os
from ctypes import wintypes
os.environ['QT_QPA_PLATFORM']='windows'
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication,QWidget
from PySide6.QtTest import QTest
from snipboard.qt_windows import WindowLayer
app=QApplication([])
w=QWidget();w.setAttribute(Qt.WA_DontShowOnScreen);w.show();app.processEvents()
layer=WindowLayer(w)
u=ctypes.WinDLL('user32',use_last_error=True)
u.GetWindowLongPtrW.argtypes=(wintypes.HWND,ctypes.c_int);u.GetWindowLongPtrW.restype=ctypes.c_ssize_t
u.SetWindowPos.argtypes=(wintypes.HWND,wintypes.HWND,ctypes.c_int,ctypes.c_int,ctypes.c_int,ctypes.c_int,wintypes.UINT)
u.GetForegroundWindow.restype=wintypes.HWND
handle=int(w.winId())
def top():return bool(u.GetWindowLongPtrW(handle,-20)&8)
try:
 layer.set_mode('top');assert top(),'initial top flag missing'
 before=u.GetForegroundWindow()
 u.SetWindowPos(handle,-2,0,0,0,0,0x13)
 assert not top()
 QTest.qWait(700)
 assert top(),'topmost not restored after Windows Z-order demotion'
 assert before==u.GetForegroundWindow(),'refresh stole foreground focus'
 layer.set_mode('normal');QTest.qWait(400);assert not top(),'cancel did not persist'
 layer.set_mode('top');w.hide();QTest.qWait(400);assert not w.isVisible(),'hidden viewer was reopened'
 w.show();QTest.qWait(400);assert top(),'reshow lost topmost'
 w.showMinimized();QTest.qWait(100)
 u.SetWindowPos(handle,-2,0,0,0,0,0x13)
 QTest.qWait(400);assert w.isMinimized(),'timer restored minimized viewer'
 w.showNormal();QTest.qWait(400);assert top(),'restoring from minimized lost topmost'
 print(json.dumps(dict(native_topmost_recovery=True,no_focus_steal=True,cancel=True,hidden_stays_hidden=True,reshow=True,minimize_restore=True)))
finally:
 layer.timer.stop();w.close()
