"""Render first-connection states with synthetic locations and an isolated library."""
import os
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
from pathlib import Path
import tempfile
from unittest.mock import patch
from PySide6.QtWidgets import QApplication
from snipboard.qt_app import Controller, configure_app
from snipboard.qt_connection import ConnectionDialog

app = QApplication.instance() or QApplication([])
configure_app(app)
output = Path(__file__).resolve().parents[1] / 'docs' / 'connection-0.5.2'
output.mkdir(exist_ok=True)
with tempfile.TemporaryDirectory() as temp:
    controller = Controller(app, None, Path(temp), native=False)
    controller.timer.stop()
    with patch.object(ConnectionDialog, 'scan', lambda self: None):
        dialog = ConnectionDialog(controller)
        dialog.show()
        dialog.found([
            dict(path='D:/Tools/Snipaste', history='D:/Tools/Snipaste/history', reason='正在运行', groups=4),
            dict(path='C:/Users/Artist/Downloads/Snipaste-2.11.3-x64', history='C:/Users/Artist/Downloads/Snipaste-2.11.3-x64/history', reason='常见位置', groups=2)])
        app.processEvents()
        dialog.grab().save(str(output / 'choose-source.png'))
        dialog.found([])
        app.processEvents()
        dialog.grab().save(str(output / 'not-found.png'))
        dialog.reject()
        app.processEvents()
    controller.quit()
    controller._finish_shutdown()
