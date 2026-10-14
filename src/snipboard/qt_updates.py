"""One nonmodal update window shared by library windows and tray."""
from pathlib import Path
import threading

from PySide6.QtCore import QObject, Signal, QUrl, Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
                               QProgressBar, QPlainTextEdit)

from . import __version__
from .runtime import app_data_root
from .updates import (SITE, check_release, download_release, version_tuple,
                      DownloadCancelled)


class UpdateService(QObject):
    changed = Signal()
    available = Signal(object)
    transfer_progress = Signal(int, int)
    transfer_source = Signal(int)

    def __init__(self, controller, *, checker=check_release, downloader=download_release, root=None):
        super().__init__(controller)
        self.controller = controller
        self.checker, self.downloader = checker, downloader
        self.root = Path(root) if root else app_data_root() / 'downloads'
        self.release = self.path = self.dialog = None
        self.checking = self.downloading = self.closed = False
        self.message = f'当前版本 {__version__}'
        self.received = self.total = self.source_index = 0
        self.cancel = threading.Event()
        self.transfer_progress.connect(self._progress)
        self.transfer_source.connect(self._source)

    def automatic_check(self):
        if not self.closed and self.controller.catalog.get_setting('updates.enabled', True):
            self.check()

    def show(self):
        if self.closed:
            return
        if self.dialog is None:
            self.dialog = UpdateDialog(self)
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()

    def manual_check(self):
        self.show()
        self.check(manual=True)

    def check(self, manual=False):
        if self.closed or self.checking or self.downloading:
            return
        self.cancel = threading.Event()
        cancel = self.cancel
        self.checking = True
        self.message = '正在检查更新…'
        self.changed.emit()
        self.controller.jobs.submit(lambda: self.checker(cancel),
            lambda release: self._checked(release, manual),
            lambda error: self._check_failed(error, manual), owner=self)

    def _checked(self, release, manual):
        self.checking = False
        if self.closed:
            return
        if self.release != release:
            self.path = None
            self.received = self.total = 0
        self.release = release
        newer = version_tuple(release.version) > version_tuple(__version__)
        skipped = self.controller.catalog.get_setting('updates.skipped') == release.version
        self.message = (f'发现新版 {release.version}' if newer else '你已在使用最新版本')
        self.changed.emit()
        if newer and (manual or not skipped):
            self.available.emit(release)
        else:
            self.available.emit(None)

    def _check_failed(self, error, manual):
        self.checking = False
        if self.closed:
            return
        self.message = str(error) if manual else '后台检查暂不可用，可手动重试'
        self.changed.emit()

    def skip(self):
        if self.release and not self.downloading:
            self.controller.catalog.set_setting('updates.skipped', self.release.version)
            self.available.emit(None)
            if self.dialog:
                self.dialog.hide()

    def download(self):
        if (self.closed or self.checking or self.downloading or not self.release or
                version_tuple(self.release.version) <= version_tuple(__version__)):
            return
        self.cancel = threading.Event()
        self.downloading = True
        self.path = None
        self.received, self.total = 0, self.release.size
        self.message = '正在连接下载服务器…'
        self.changed.emit()
        release, cancel = self.release, self.cancel
        self.controller.jobs.submit(
            lambda: self.downloader(release, self.root, cancel,
                                    self.transfer_progress.emit, self.transfer_source.emit),
            self._downloaded, self._download_failed, owner=self)

    def cancel_download(self):
        if self.downloading:
            self.cancel.set()
            self.message = '正在取消下载…'
            self.changed.emit()

    def _source(self, index):
        if self.closed or not self.downloading or self.cancel.is_set():
            return
        self.source_index = index
        self.message = '正在从官网下载…' if index == 0 else '主下载源暂不可用，正在尝试备用源…'
        self.changed.emit()

    def _progress(self, received, total):
        if self.closed or not self.downloading:
            return
        self.received, self.total = received, total
        self.changed.emit()

    def _downloaded(self, path):
        self.downloading = False
        if self.closed:
            return
        self.path = Path(path)
        self.message = '下载完成，SHA256 校验通过'
        self.changed.emit()

    def _download_failed(self, error):
        self.downloading = False
        if self.closed:
            return
        self.message = '已取消下载，可重新下载' if isinstance(error, DownloadCancelled) else str(error)
        self.changed.emit()

    def shutdown(self):
        self.closed = True
        self.cancel.set()
        if self.dialog:
            self.dialog.hide()


class UpdateDialog(QDialog):
    def __init__(self, service):
        # Standalone window survives closing an individual library window.
        super().__init__()
        self.service = service
        self.setWindowTitle('SnipBoard · 软件更新')
        self.setMinimumWidth(520)
        layout = QVBoxLayout(self)
        title = QLabel('让参考图继续陪你画画')
        title.setObjectName('title')
        layout.addWidget(title)
        layout.addWidget(QLabel(f'当前版本：{__version__} · Windows x64 便携版'))
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.PlainText)
        layout.addWidget(self.status)
        self.notes = QPlainTextEdit()
        self.notes.setReadOnly(True)
        self.notes.setMaximumHeight(145)
        layout.addWidget(self.notes)
        self.progress = QProgressBar()
        layout.addWidget(self.progress)
        self.details = QLabel()
        layout.addWidget(self.details)
        self.instructions = QLabel('下载后：退出 SnipBoard → 完整解压新版本到新文件夹 → 运行 SnipBoard.exe。\n'
                                   '默认图库保存在电脑的应用数据目录，更换程序文件夹不会清空图库。\n'
                                   '若使用自定义图库，请继续选择原图库路径。')
        self.instructions.setWordWrap(True)
        layout.addWidget(self.instructions)
        row = QHBoxLayout()
        self.check_button = QPushButton('检查更新')
        self.download_button = QPushButton('下载新版')
        self.cancel_button = QPushButton('取消下载')
        self.folder_button = QPushButton('打开下载文件夹')
        for button in (self.check_button, self.download_button, self.cancel_button, self.folder_button):
            row.addWidget(button)
        layout.addLayout(row)
        row = QHBoxLayout()
        self.skip_button = QPushButton('跳过此版本')
        website = QPushButton('访问官网')
        close = QPushButton('关闭')
        row.addWidget(self.skip_button)
        row.addStretch()
        row.addWidget(website)
        row.addWidget(close)
        layout.addLayout(row)
        self.check_button.clicked.connect(lambda: service.check(manual=True))
        self.download_button.clicked.connect(service.download)
        self.cancel_button.clicked.connect(service.cancel_download)
        self.folder_button.clicked.connect(self.open_folder)
        self.skip_button.clicked.connect(service.skip)
        website.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(SITE)))
        close.clicked.connect(self.hide)
        service.changed.connect(self.refresh)
        self.refresh()

    def open_folder(self):
        if self.service.path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.service.path.parent)))

    def refresh(self):
        service = self.service
        release = service.release
        newer = release and version_tuple(release.version) > version_tuple(__version__)
        busy = service.downloading or service.checking
        self.status.setText(service.message)
        self.notes.setPlainText(release.notes if release else '可在这里查看更新说明、下载并校验新版。')
        self.check_button.setEnabled(not busy)
        self.download_button.setVisible(not service.path)
        self.download_button.setEnabled(bool(newer) and not busy)
        self.cancel_button.setVisible(service.downloading)
        self.cancel_button.setEnabled(not service.cancel.is_set())
        self.folder_button.setVisible(bool(service.path))
        self.skip_button.setVisible(bool(newer) and not service.path)
        self.skip_button.setEnabled(not busy)
        self.progress.setVisible(service.downloading or bool(service.path))
        self.progress.setRange(0, 100)
        self.progress.setValue(min(100, round(service.received * 100 / service.total)) if service.total else 0)
        self.details.setText(f'{service.received / 1048576:.1f} / {service.total / 1048576:.1f} MB'
                             if service.total else '')
        self.instructions.setVisible(bool(service.path))

    def closeEvent(self, event):
        # Downloads continue while hidden; cancellation is explicit.
        event.ignore()
        self.hide()
