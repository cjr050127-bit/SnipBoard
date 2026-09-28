"""Qt background jobs and bounded, lazy image decoding shared by every window."""
from __future__ import annotations

from collections import OrderedDict
import logging
import threading
import weakref
from shiboken6 import isValid

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, QSize, Qt
from PySide6.QtGui import QImageReader, QPixmap

STYLE = '''
QWidget { color: #e3e5e9; font-family: "Microsoft YaHei UI", "Segoe UI"; font-size: 13px; }
QMainWindow, QDialog, QScrollArea, QWidget#filterSurface, QWidget#dockSurface { background: #17191d; }
QMenuBar, QToolBar { background: #202328; border: none; padding: 5px; spacing: 8px; }
QMenuBar::item:selected, QMenu::item:selected { background: #38454e; }
QMenu { background: #252a30; border: 1px solid #414850; padding: 6px; }
QMenu::item { padding: 7px 30px 7px 14px; }
QMenu::separator { height: 1px; background: #414850; margin: 5px; }
QListView, QTreeWidget, QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox { background: #202328; border: 1px solid #343940; border-radius: 5px; padding: 5px; }
QDoubleSpinBox:disabled { color: #697582; background: #1b1e23; }
QLineEdit:focus { border: 1px solid #7cafbc; }
QListView::item:selected, QTreeWidget::item:selected { background: #34434c; }
QPushButton, QToolButton { background: #2a3037; border: 1px solid #3c444e; border-radius: 5px; padding: 6px 12px; }
QPushButton:hover, QToolButton:hover { background: #3a454f; }
QPushButton:checked { border: 2px solid #b1dce7; }
QLabel#section { color: #929ba7; font-size: 11px; font-weight: bold; padding-top: 14px; }
QLabel#title { font-size: 20px; font-weight: bold; }
QStatusBar { color: #a1a8b3; background: #202328; }
QScrollBar:vertical { background: #1c1f24; width: 10px; }
QScrollBar::handle:vertical { background: #4b525e; min-height: 25px; border-radius: 4px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }
QDockWidget { background: #17191d; }
QDockWidget::title { background: #262c33; padding: 7px 8px; text-align: left; }
QMainWindow::separator { background: #30363e; width: 5px; height: 5px; }
QMainWindow::separator:hover { background: #7cafbc; }
QTabBar::tab { background: #202328; color: #b4becb; padding: 6px; border: 1px solid #343940; }
QTabBar::tab:selected { background: #34434c; color: #ffffff; }
QSplitter::handle { background: #30363e; width: 1px; }
QToolTip { background: #313840; color: white; border: none; padding: 5px; }
'''


class JobSignals(QObject):
    finished = Signal(int, bool, object)


class Job(QRunnable):
    def __init__(self, number, function):
        super().__init__()
        self.number, self.function = number, function
        self.signals = JobSignals()
        self.cancelled = threading.Event()

    def run(self):
        try:
            self.signals.finished.emit(self.number, True, None if self.cancelled.is_set() else self.function())
        except Exception as error:
            logging.getLogger(__name__).exception('Background job failed')
            self.signals.finished.emit(self.number, False, error)


class Jobs(QObject):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(3)
        self.pending, self.counter = {}, 0
        self.latest = {}

    def submit(self, function, done=lambda result: None, failed=lambda error: None, *, owner=None, key=None):
        self.counter += 1
        job = Job(self.counter, function)
        token = (id(owner), key) if key is not None else None
        previous = self.pending.get(self.latest.get(token)) if token else None
        if previous:
            previous[0].cancelled.set()
        if token:
            self.latest[token] = self.counter
        self.pending[self.counter] = (job, done, failed, weakref.ref(owner) if owner is not None else None, token)
        job.signals.finished.connect(self._complete)
        self.pool.start(job)
        return self.counter

    def _complete(self, number, success, result):
        entry = self.pending.pop(number, None)
        if entry:
            job, done, failed, owner_ref, token = entry
            if token and self.latest.get(token) == number:
                self.latest.pop(token, None)
            owner = owner_ref() if owner_ref else None
            if job.cancelled.is_set() or (owner_ref and (owner is None or not isValid(owner))):
                return
            (done if success else failed)(result)


def decode_image(path, bound):
    reader = QImageReader(path)
    reader.setAutoTransform(True)
    size = reader.size()
    if size.isValid() and max(size.width(), size.height()) > bound:
        size.scale(QSize(bound, bound), Qt.KeepAspectRatio)
        reader.setScaledSize(size)
    image = reader.read()
    if image.isNull():
        raise ValueError(reader.errorString())
    return image


class ImageStore(QObject):
    ready = Signal(str)

    def __init__(self, jobs, parent=None):
        super().__init__(parent)
        self.jobs = jobs
        self.cache, self.loading, self.failed = OrderedDict(), set(), set()
        self.bytes = 0
        self.limit = 160 * 1024 * 1024

    def request(self, path, bound=800):
        key = (path, bound)
        if key in self.cache:
            self.cache.move_to_end(key)
            return self.cache[key]
        if key not in self.loading and key not in self.failed and len(self.loading) < 48:
            self.loading.add(key)
            self.jobs.submit(lambda: decode_image(path, bound),
                             lambda value: self._loaded(key, value),
                             lambda error: self._failed(key))
        return None

    def _failed(self, key):
        self.loading.discard(key)
        self.failed.add(key)
        self.ready.emit(key[0])

    def _loaded(self, key, value):
        self.loading.discard(key)
        pixmap = QPixmap.fromImage(value)
        self.cache[key] = pixmap
        self.bytes += pixmap.width() * pixmap.height() * 4
        while self.bytes > self.limit and len(self.cache) > 1:
            _, expired = self.cache.popitem(last=False)
            self.bytes -= expired.width() * expired.height() * 4
        self.ready.emit(key[0])
