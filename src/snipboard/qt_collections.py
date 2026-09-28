"""Internal-only thumbnail drag and drop for local reference groups."""
import json
from PySide6.QtCore import Qt, Signal, QMimeData, QPoint, QRectF
from PySide6.QtGui import QDrag, QPixmap, QPainter, QColor, QPen
from PySide6.QtWidgets import QListView, QListWidget, QLabel

MIME = 'application/x-snipboard-images'


def decode_drop(mime):
    if not mime.hasFormat(MIME):
        return []
    try:
        data = json.loads(bytes(mime.data(MIME)).decode('utf-8'))
    except (ValueError, UnicodeError):
        return []
    if not isinstance(data, list) or len(data) > 10000:
        return []
    return list(dict.fromkeys(d for d in data if isinstance(d, str) and len(d) == 64 and all(c in '0123456789abcdef' for c in d)))


class PhotoGrid(QListView):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._drag_started = False
        self.setDragEnabled(True)
        self.setDragDropMode(QListView.DragOnly)
        self.setSelectionMode(QListView.ExtendedSelection)
        self.setDefaultDropAction(Qt.CopyAction)

    def setMovement(self, movement):
        # QListView::setMovement(Static) disables dragEnabled as a side effect.
        # Fixed thumbnail positions must not disable copying images to reference groups.
        super().setMovement(movement)
        self.setDragDropMode(QListView.DragOnly)

    def mousePressEvent(self, event):
        self._drag_started = False
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        if self._drag_started:
            self._drag_started = False
            self.setState(QListView.NoState)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def drag_preview(self, row, count):
        ratio = self.devicePixelRatioF()
        preview = QPixmap(round(100 * ratio), round(92 * ratio))
        preview.setDevicePixelRatio(ratio)
        preview.fill(Qt.transparent)
        painter = QPainter(preview)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setBrush(QColor(30, 39, 47, 230))
        painter.setPen(QPen(QColor('#a4d8e6'), 1.5))
        painter.drawRoundedRect(QRectF(1, 1, 98, 90), 7, 7)
        image = QPixmap(row['thumbnail'])
        if not image.isNull():
            size = image.size().scaled(round(86*ratio), round(74*ratio), Qt.KeepAspectRatio)
            image = image.scaled(size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            image.setDevicePixelRatio(ratio)
            logical = image.deviceIndependentSize()
            painter.drawPixmap(QPoint(round((100-logical.width())/2), round((92-logical.height())/2)), image)
        if count > 1:
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor('#295c6b'))
            painter.drawRoundedRect(QRectF(48, 69, 47, 19), 5, 5)
            painter.setPen(Qt.white)
            painter.drawText(QRectF(48, 69, 47, 19), Qt.AlignCenter, str(count))
        painter.end()
        return preview

    def startDrag(self, actions):
        indexes = self.selectedIndexes()
        if not indexes:
            return
        self._drag_started = True
        digests = [i.data(Qt.UserRole)['digest'] for i in indexes]
        mime = QMimeData()
        mime.setData(MIME, json.dumps(digests).encode('utf-8'))
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.setPixmap(self.drag_preview(indexes[0].data(Qt.UserRole), len(indexes)))
        drag.setHotSpot(QPoint(12, 12))
        try:
            drag.exec(Qt.CopyAction)
        finally:
            drag.deleteLater()


class WorkGroupList(QListWidget):
    imagesDropped = Signal(object, object)

    def __init__(self, allow_empty=True):
        super().__init__()
        self.allow_empty = allow_empty
        self.setAcceptDrops(True)
        self.setDragDropMode(QListWidget.DropOnly)
        self.setDefaultDropAction(Qt.CopyAction)
        self.setDropIndicatorShown(True)
        self.setToolTip('拖入缩略图即可加入；空白处放开则直接创建参考组。双击组名可重命名。')

    def dragEnterEvent(self, event):
        if decode_drop(event.mimeData()):
            event.setDropAction(Qt.CopyAction)
            event.accept()

    def dragMoveEvent(self, event):
        item = self.itemAt(event.position().toPoint())
        if decode_drop(event.mimeData()) and (item is not None or self.allow_empty):
            self.setCurrentItem(item)
            event.setDropAction(Qt.CopyAction)
            event.accept()
        else:
            event.ignore()

    def dropEvent(self, event):
        digests = decode_drop(event.mimeData())
        if digests:
            item = self.itemAt(event.position().toPoint())
            if item is None and not self.allow_empty:
                event.ignore()
                return
            self.imagesDropped.emit(item.data(Qt.UserRole) if item else None, digests)
            event.setDropAction(Qt.CopyAction)
            event.accept()


class NewGroupDrop(QLabel):
    imagesDropped = Signal(object, object)

    def __init__(self):
        super().__init__('＋ 拖图片到这里建立参考组')
        self.setAlignment(Qt.AlignCenter)
        self.setWordWrap(True)
        self.setMinimumHeight(54)
        self.setAcceptDrops(True)
        self.highlight(False)

    def highlight(self, active):
        self.setStyleSheet('border:1px dashed ' + ('#b3f1d6' if active else '#718492') +
                          '; border-radius:7px; padding:8px; color:#a8c5d2;' +
                          ('background:#244940;' if active else ''))

    def dragEnterEvent(self, event):
        if decode_drop(event.mimeData()):
            self.highlight(True)
            event.setDropAction(Qt.CopyAction)
            event.accept()

    def dragMoveEvent(self, event):
        if decode_drop(event.mimeData()):
            event.setDropAction(Qt.CopyAction)
            event.accept()

    def dragLeaveEvent(self, event):
        self.highlight(False)
        event.accept()

    def dropEvent(self, event):
        self.highlight(False)
        digests = decode_drop(event.mimeData())
        if digests:
            self.imagesDropped.emit(None, digests)
            event.setDropAction(Qt.CopyAction)
            event.accept()
