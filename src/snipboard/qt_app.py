"""Shared desktop controller. One service, multiple libraries, independent board."""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
import sys
import threading

from PySide6.QtCore import QObject, QTimer, Signal, Qt
from PySide6.QtGui import QIcon, QPixmap, QPainter, QColor, QFont, QFontDatabase
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (QApplication, QSystemTrayIcon, QMenu, QFileDialog, QMessageBox,
                               QDialog, QVBoxLayout, QLabel, QPushButton, QCheckBox, QDialogButtonBox)

from .catalog import Catalog
from .source import normalize_source
from .qt_support import Jobs, ImageStore, STYLE
from .qt_windows import NativeHotkey


def configure_app(app):
    app.setApplicationName('SnipBoard')
    app.setOrganizationName('SnipBoard')
    app.setStyle('Fusion')
    if not QFontDatabase.families():
        # The offscreen Qt platform does not enumerate Windows fonts itself.
        fonts = Path(os.environ.get('SystemRoot', 'C:/Windows')) / 'Fonts'
        for filename in ('msyh.ttc', 'msyhbd.ttc', 'segoeui.ttf'):
            if (fonts / filename).exists():
                QFontDatabase.addApplicationFont(str(fonts / filename))
    app.setFont(QFont('Microsoft YaHei UI', 10))
    app.setStyleSheet(STYLE)
    app.setWindowIcon(app_icon())
    app.setQuitOnLastWindowClosed(False)


def app_icon():
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setBrush(QColor('#8cbecb'))
    painter.setPen(Qt.NoPen)
    painter.drawRoundedRect(3, 3, 58, 58, 14, 14)
    painter.setPen(QColor('#172127'))
    painter.setFont(QFont('Segoe UI', 31, QFont.Bold))
    painter.drawText(pixmap.rect(), Qt.AlignCenter, 'S')
    painter.end()
    return QIcon(pixmap)


class Controller(QObject):
    changed = Signal()
    status = Signal(str)
    progress = Signal(int, str)

    def __init__(self, app, source, root, native=True):
        super().__init__()
        self.app, self.source = app, Path(source).resolve() if source else None
        self.catalog = Catalog(root)
        self.jobs = Jobs(self)
        self.images = ImageStore(self.jobs, self)
        self.windows, self.closed_states = [], []
        self.board, self.tray, self.hotkey = None, None, None
        self.connection_dialog = None
        self.sync_running = self.quitting = False
        self.shutdown_finished = False
        self.cancel_sync = threading.Event()
        self.progress.connect(lambda count, name: self.status.emit(f'正在索引贴图 · {count} 张 · {name}'))
        self.fingerprint = None
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.sync)
        self.timer.start(5000)
        self.auto_sync = self.catalog.get_setting('auto_sync', True)
        if self.source:
            self.catalog.set_setting('source', str(self.source))
        if native:
            self.hotkey = NativeHotkey(app, self.show_board)
            if QSystemTrayIcon.isSystemTrayAvailable():
                self.tray = QSystemTrayIcon(app.windowIcon(), self)
                self.tray.setToolTip('SnipBoard · Ctrl+Alt+B 打开当前工作组')
                menu = QMenu()
                menu.addAction('打开图片库', self.new_library)
                menu.addAction('参考图查看器  Ctrl+Alt+B', self.show_board)
                menu.addAction('刷新贴图图组', self.sync)
                menu.addAction('设置', self.settings)
                menu.addSeparator()
                menu.addAction('退出应用', self.quit)
                self.tray.setContextMenu(menu)
                self.tray.activated.connect(lambda reason: self.new_library() if reason == QSystemTrayIcon.DoubleClick else None)
                self.tray.show()

    def new_library(self, state=None):
        from .qt_library import LibraryWindow
        if self.quitting:
            return
        window = LibraryWindow(self, state if isinstance(state, dict) else None)
        self.windows.append(window)
        window.show()
        self.ensure_onscreen(window)
        return window

    def ensure_onscreen(self, window):
        if not any(screen.availableGeometry().intersects(window.frameGeometry()) for screen in self.app.screens()):
            window.move(self.app.primaryScreen().availableGeometry().topLeft())

    def show_board(self, group_id=None):
        from .qt_board import BoardWindow
        if self.quitting:
            return
        if self.board is None:
            self.board = BoardWindow(self)
        self.board.update_groups()
        if group_id is None or isinstance(group_id, bool):
            current = self.catalog.get_setting('work.current')
            if current and any(g['id'] == current for g in self.catalog.work_groups()):
                group_id = f'work:{current}'
        if (isinstance(group_id, (int, str)) and not isinstance(group_id, bool)
                and group_id != self.board.board_id):
            self.board.load_group(group_id)
        self.board.showNormal() if self.board.isMinimized() else self.board.show()
        self.ensure_onscreen(self.board)
        self.board.raise_()
        self.board.activateWindow()
        self.board.setFocus()
        self.board.layer.refresh()
        return self.board

    def delete_images(self, digests):
        count = self.catalog.delete_images(digests)
        self.changed.emit()
        if self.board:
            self.board.update_groups()
        self.status.emit(f'已从图片库删除 {count} 张图片，源文件保留')
        return count

    def sync(self, manual=True):
        if self.sync_running or self.quitting or not self.source:
            return
        # Timeout signals have no arguments; use the timer's sender to respect auto-sync.
        if self.sender() is self.timer and not self.auto_sync and not self.catalog.get_setting('work.current'):
            return
        self.sync_running = True
        self.status.emit('正在读取贴图图组…')
        root, source = self.catalog.root, self.source
        def work():
            with Catalog(root) as catalog:
                report = catalog.sync_source(source, self.cancel_sync.is_set, self.progress.emit)
                signature = [tuple(row) for row in catalog.db.execute('SELECT path,digest,present FROM source_files ORDER BY path')]
                names = [tuple(row) for row in catalog.db.execute('SELECT id,name FROM boards ORDER BY id')]
                return report, (signature, names)
        self.jobs.submit(work, self._synced, self._sync_failed)

    def _synced(self, result):
        self.sync_running = False
        if self.quitting:
            return
        report, fingerprint = result
        if fingerprint != self.fingerprint:
            self.fingerprint = fingerprint
            self.images.failed.clear()
            self.changed.emit()
            if self.board:
                self.board.update_groups()
        text = f"{report['groups']} 个贴图图组 · {report['files']} 个源图片文件 · 索引已更新"
        if not report['files'] and not report['errors']:
            text = '已连接 Snipaste，尚无已保存贴图；请先保存贴图图组，应用会自动同步'
        if report.get('auto_added'):
            text += f" · {report['auto_added']} 张新贴图已加入工作组"
        if report['errors']:
            text += f" · {len(report['errors'])} 个问题（设置中查看）"
        if self.hotkey and not self.hotkey.registered:
            text += ' · Ctrl+Alt+B 被占用，可从工具栏或托盘打开查看器'
        self.status.emit(text)

    def _sync_failed(self, error):
        self.sync_running = False
        self.status.emit(f'同步失败：{error}')

    def choose_source(self):
        self.show_connection()

    def show_connection(self, first_run=False):
        from .qt_connection import ConnectionDialog
        if self.quitting:
            return
        if self.connection_dialog is None:
            self.connection_dialog = ConnectionDialog(self, first_run)
            self.connection_dialog.finished.connect(lambda _: setattr(self, 'connection_dialog', None))
        self.connection_dialog.show()
        self.connection_dialog.raise_()
        self.connection_dialog.activateWindow()

    def start_connection(self):
        if self.source:
            source = self.source
            def ready(path):
                if not self.quitting and self.source == source:
                    self.source = path
                    self.catalog.set_setting('source', str(path))
                    self.sync()
            def unavailable(_error):
                if not self.quitting and self.source == source:
                    self.status.emit('原 Snipaste 位置暂不可用；本地图片仍可查看')
                    self.show_connection()
            self.jobs.submit(lambda: normalize_source(source), ready, unavailable, owner=self, key='source-check')
        elif not self.catalog.get_setting('connection.setup_done', False):
            self.show_connection(first_run=True)
        else:
            self.status.emit('可从“文件 → 连接 Snipaste…”连接贴图，或导入本地图片')

    def settings(self):
        dialog = QDialog(self.windows[-1] if self.windows else None)
        dialog.setWindowTitle('设置')
        dialog.setMinimumWidth(500)
        layout = QVBoxLayout(dialog)
        source = QLabel('贴图来源：' + (str(self.source) if self.source else '尚未选择'))
        source.setWordWrap(True)
        layout.addWidget(source)
        library = QLabel('本地图库：' + str(self.catalog.root))
        library.setWordWrap(True)
        layout.addWidget(library)
        choose = QPushButton('连接／更改 Snipaste 来源…')
        choose.clicked.connect(self.choose_source)
        layout.addWidget(choose)
        auto = QCheckBox('后台自动同步贴图图组（每 5 秒）')
        auto.setChecked(self.auto_sync)
        layout.addWidget(auto)
        layout.addWidget(QLabel('相似推荐：本地色彩与构图；无模型下载，无图片上传。'))
        layout.addWidget(QLabel('Ctrl+Alt+B：优先打开当前工作组；未设置时恢复上次标签或参考组。'))
        layout.addWidget(QLabel('当前工作组启用时，每 5 秒检查新增贴图并自动收集。'))
        hotkey = '可用' if self.hotkey and self.hotkey.registered else '未注册或被占用'
        layout.addWidget(QLabel('全局快捷键状态：' + hotkey))
        layout.addWidget(QLabel('关闭窗口后保留托盘；从托盘选择“退出应用”可完全退出。'))
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() == QDialog.Accepted:
            self.auto_sync = auto.isChecked()
            self.catalog.set_setting('auto_sync', self.auto_sync)

    def diagnostics(self):
        report = self.catalog.get_setting('last_sync', {})
        issues = report.get('errors', [])
        text = f"来源：{self.source}\n贴图图组：{report.get('groups', 0)}\n源图片文件：{report.get('files', 0)}\n"
        text += '范围：仅贴图图组 PNG；不读取截图历史。\n成员依据：图组目录文件清单。\n'
        text += '\n'.join(f"{row['file']}: {row['error']}" for row in issues[:20]) or '\n无导入错误。'
        warnings = report.get('warnings', [])
        if warnings:
            text += '\n名称元数据提示：\n' + '\n'.join(warnings)
        QMessageBox.information(self.windows[-1] if self.windows else None, '索引状态', text)

    def tile(self, horizontal):
        if not self.windows:
            return
        rect = self.windows[-1].screen().availableGeometry()
        count = len(self.windows)
        for index, window in enumerate(self.windows):
            window.showNormal()
            if horizontal:
                window.setGeometry(rect.x() + index * rect.width() // count, rect.y(), rect.width() // count, rect.height())
            else:
                window.setGeometry(rect.x(), rect.y() + index * rect.height() // count, rect.width(), rect.height() // count)

    def restore_windows(self):
        states = self.catalog.get_setting('windows', [])
        for index, state in enumerate(states):
            if index < len(self.windows):
                self.windows[index].restore_state(state)
                self.windows[index].refresh()
                self.ensure_onscreen(self.windows[index])
            else:
                self.new_library(state)

    def window_closed(self, window):
        self.closed_states.append(window.state())
        if window in self.windows:
            self.windows.remove(window)
        if not self.windows and not self.quitting:
            self.catalog.set_setting('windows', self.closed_states[-8:])
            if self.tray is None:
                self.quit()

    def quit(self):
        if self.quitting:
            return
        self.quitting = True
        self.timer.stop()
        self.cancel_sync.set()
        if self.connection_dialog is not None:
            self.connection_dialog.reject()
        states = [w.state() for w in self.windows] or self.closed_states[-8:]
        self.catalog.set_setting('windows', states)
        if self.board:
            self.board.save_timer.stop()
            self.board.layer.timer.stop()
            self.board.save()
            self.board.hide()
        for window in list(self.windows):
            window.close()
        if self.hotkey:
            self.hotkey.close()
        if self.tray:
            self.tray.hide()
        # Keep the event loop alive until owned worker jobs stop; never destroy running Qt jobs.
        self.shutdown_timer = QTimer(self)
        self.shutdown_timer.timeout.connect(self._finish_shutdown)
        self.shutdown_timer.start(50)

    def _finish_shutdown(self):
        if not self.jobs.pending and not self.shutdown_finished:
            self.shutdown_finished = True
            self.shutdown_timer.stop()
            self.catalog.close()
            self.app.quit()


def run(source: Path | None, library_root: Path):
    app = QApplication.instance() or QApplication(sys.argv[:1])
    configure_app(app)
    # Server name is specific to the library, allowing isolated test installations.
    server_name = 'SnipBoard-' + hashlib.sha256(str(library_root.resolve()).casefold().encode()).hexdigest()[:16]
    socket = QLocalSocket()
    socket.connectToServer(server_name)
    if socket.waitForConnected(350):
        socket.write(b'open')
        socket.flush()
        socket.waitForBytesWritten(350)
        socket.disconnectFromServer()
        return
    server = QLocalServer()
    if not server.listen(server_name):
        raise OSError('无法创建 SnipBoard 本地通信服务')
    if source is None:
        with Catalog(library_root) as catalog:
            stored = catalog.get_setting('source')
        if isinstance(stored, str) and stored:
            source = Path(stored)
    controller = Controller(app, source, library_root)
    def connected():
        connection = server.nextPendingConnection()
        controller.new_library()
        if connection:
            connection.disconnectFromServer()
            connection.deleteLater()
    server.newConnection.connect(connected)
    states = controller.catalog.get_setting('windows', [])
    if states:
        for state in states[:8]:
            controller.new_library(state)
    else:
        controller.new_library()
    QTimer.singleShot(50, controller.start_connection)
    app.exec()
    server.close()
