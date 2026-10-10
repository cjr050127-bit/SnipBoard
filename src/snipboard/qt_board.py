"""Text-free reference board, with all controls reachable from its context menu."""
from __future__ import annotations

import copy
import math
import threading
from pathlib import Path

from PySide6.QtCore import Qt, QRect, QRectF, QPointF, QTimer
from PySide6.QtGui import QAction, QActionGroup, QColor, QKeySequence, QPainter, QPen, QPixmap, QCursor, QPolygonF
from PySide6.QtWidgets import (QGraphicsView, QGraphicsScene, QGraphicsRectItem, QMenu,
                               QInputDialog, QMessageBox, QDialog, QDialogButtonBox,
                               QFormLayout, QSpinBox)

from .layouts import arrange
from .packing import compact
from .qt_windows import WindowLayer


class PictureItem(QGraphicsRectItem):
    def __init__(self, row, images, width=260):
        super().__init__(0, 0, width, width * row['height'] / max(row['width'], 1))
        self.row, self.images, self.resizing = row, images, False
        self.rotating = False
        self.setFlags(self.GraphicsItemFlag.ItemIsMovable | self.GraphicsItemFlag.ItemIsSelectable)
        self.setAcceptHoverEvents(True)
        self.setPen(QPen(Qt.NoPen))
        self.setTransformOriginPoint(self.rect().center())

    @staticmethod
    def rotation_cursor():
        pixmap = QPixmap(32, 32)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)
        for color, width in [('black', 5), ('white', 2)]:
            painter.setPen(QPen(QColor(color), width))
            painter.drawArc(6, 6, 20, 20, 40 * 16, 285 * 16)
        painter.setPen(QPen(Qt.black, 1))
        painter.setBrush(Qt.white)
        painter.drawPolygon(QPolygonF([QPointF(24, 21), QPointF(31, 19), QPointF(28, 28)]))
        painter.end()
        return QCursor(pixmap, 16, 16)

    def corner_mode(self, point):
        scale = abs(self.scene().views()[0].transform().m11())
        radius = min(14 / max(scale, .01), min(self.rect().width(), self.rect().height()) / 3)
        if (point - self.rect().bottomRight()).manhattanLength() <= radius * 2:
            return 'rotate'
        if self.isSelected() and (point - self.rect().bottomLeft()).manhattanLength() <= radius * 2:
            return 'resize'
        return None

    def update_hover(self, point):
        mode = self.corner_mode(point)
        if mode == 'rotate':
            self.setCursor(self.rotation_cursor())
        elif mode == 'resize':
            self.setCursor(Qt.SizeBDiagCursor)
        else:
            self.unsetCursor()

    def hoverMoveEvent(self, event):
        self.update_hover(event.pos())
        super().hoverMoveEvent(event)

    def set_width(self, width):
        anchor = self.mapToScene(QPointF(0, 0))
        self.setRect(0, 0, width, width * self.row['height'] / max(self.row['width'], 1))
        self.setTransformOriginPoint(self.rect().center())
        self.setPos(self.pos() + anchor - self.mapToScene(QPointF(0, 0)))

    def paint(self, painter, option, widget=None):
        scale = math.hypot(painter.transform().m11(), painter.transform().m12())
        level = scale * self.rect().width()
        path = self.row['thumbnail'] if level <= 800 else self.row['asset']
        image = self.images.request(path, 800 if level <= 800 else 4096)
        if image:
            painter.setRenderHint(QPainter.SmoothPixmapTransform)
            painter.drawPixmap(self.rect(), image, QRectF(image.rect()))
        if self.isSelected():
            pen = QPen(QColor('#a4d2df'), 1)
            pen.setCosmetic(True)
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            painter.drawRect(self.rect())
            size = 7 / max(scale, .01)
            painter.fillRect(QRectF(0, self.rect().bottom()-size, size, size), QColor('#a4d2df'))
            painter.drawArc(QRectF(self.rect().right()-size*2, self.rect().bottom()-size*2, size*2, size*2), 25*16, 290*16)

    def mousePressEvent(self, event):
        mode = self.corner_mode(event.pos())
        if event.button() != Qt.LeftButton:
            super().mousePressEvent(event)
        elif mode == 'rotate':
            if not self.isSelected():
                if not event.modifiers() & Qt.ControlModifier:
                    self.scene().clearSelection()
                self.setSelected(True)
            self.rotating = True
            self.rotation_center = self.mapToScene(self.rect().center())
            delta = event.scenePos() - self.rotation_center
            self.last_pointer_angle = math.degrees(math.atan2(delta.y(), delta.x()))
            self.rotation_delta = 0
            self.rotation_starts = {p: p.rotation() for p in self.scene().selectedItems()}
            event.accept()
        elif mode == 'resize':
            self.resizing = True
            self.resize_center = self.mapToScene(self.rect().center())
            delta = event.scenePos() - self.resize_center
            self.resize_distance = max(1, math.hypot(delta.x(), delta.y()))
            self.resize_width = self.rect().width()
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.rotating:
            delta = event.scenePos() - self.rotation_center
            if math.hypot(delta.x(), delta.y()) < 2:
                return
            angle = math.degrees(math.atan2(delta.y(), delta.x()))
            self.rotation_delta += (angle - self.last_pointer_angle + 180) % 360 - 180
            self.last_pointer_angle = angle
            change = round(self.rotation_delta / 15) * 15 if event.modifiers() & Qt.ShiftModifier else self.rotation_delta
            for picture, initial in self.rotation_starts.items():
                picture.setRotation((initial + change) % 360)
            event.accept()
        elif self.resizing:
            delta = event.scenePos() - self.resize_center
            width = max(40., min(4000., self.resize_width * math.hypot(delta.x(), delta.y()) / self.resize_distance))
            self.set_width(width)
            self.setPos(self.pos() + self.resize_center - self.mapToScene(self.rect().center()))
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self.rotating or self.resizing:
            self.rotating = self.resizing = False
            event.accept()
            return
        self.resizing = False
        super().mouseReleaseEvent(event)


class BoardWindow(QGraphicsView):
    COMPACT_MARGIN = 3
    BACKGROUNDS = {'black': ('黑色', '#000000'), 'white': ('白色', '#ffffff'),
                   'gray': ('灰色', '#808080'), 'transparent': ('透明', None)}

    def __init__(self, controller):
        self.scene_model = QGraphicsScene()
        super().__init__(self.scene_model)
        self.controller, self.catalog, self.images = controller, controller.catalog, controller.images
        self.groups, self.pictures, self.board_id = [], {}, None
        self.undo_states, self.undo_index = [], -1
        self.pan_start = None
        self.space = False
        self.window_drag = None
        self.window_tool = None
        self.background_mode = 'black'
        self.glow_timer = QTimer(self)
        self.glow_timer.setSingleShot(True)
        self.glow_timer.timeout.connect(lambda: self.viewport().update())
        self.suppress_context = False
        self.arrange_cancel = None
        self.arrange_generation = 0
        self.arrange_result = None
        self.gap = int(self.catalog.get_setting('board.gap', 18))
        # Compact spacing is in viewport pixels, independent of the old scene
        # spacing used by ordered layouts. Existing libraries get the new default.
        self.compact_gap = max(0, min(50, self.catalog.get_setting('board.compact_gap', 3)))
        self.setWindowTitle('SnipBoard · 参考图查看器')
        self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint)
        # Establish the alpha-capable native surface before its first show. Keep
        # it across color changes so switching does not recreate the window.
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFrameShape(self.Shape.NoFrame)
        self.setStyleSheet('QGraphicsView { background: transparent; border: none; }')
        self.viewport().setAutoFillBackground(False)
        self.viewport().setAttribute(Qt.WA_OpaquePaintEvent, False)
        self.viewport().setAttribute(Qt.WA_NoSystemBackground)
        self.set_background(self.catalog.get_setting('board.background', 'black'), persist=False)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        self.setTransformationAnchor(self.ViewportAnchor.AnchorUnderMouse)
        self.setDragMode(self.DragMode.RubberBandDrag)
        self.resize(980, 700)
        self.setMinimumSize(280, 200)
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)
        self.layer = WindowLayer(self)
        self.images.ready.connect(lambda _: self.viewport().update())
        self.commands = {}
        self._commands()
        self.layer.changed.connect(self.sync_layer_action)
        geometry = self.catalog.get_setting('board.geometry')
        if (isinstance(geometry, list) and len(geometry) == 4 and
                all(isinstance(n, int) and abs(n) < 100000 for n in geometry) and min(geometry[2:]) > 0):
            self.setGeometry(*geometry)
        mode = self.catalog.get_setting('board.layer', 'top')
        if not self.catalog.get_setting('board.top_default_migrated', False):
            # Older builds defaulted to normal. Adopt the new default once;
            # subsequent explicit cancellation is retained across restarts.
            if mode == 'normal':
                mode = 'top'
            self.catalog.set_setting('board.layer', mode)
            self.catalog.set_setting('board.top_default_migrated', True)
        self.layer.set_mode(mode)
        self.save_timer = QTimer(self)
        self.save_timer.setSingleShot(True)
        self.save_timer.timeout.connect(self.save)

    def set_background(self, mode, persist=True):
        if mode not in self.BACKGROUNDS:
            mode = 'black'
        self.background_mode = mode
        # Windows passes pointer input through alpha-zero pixels. A 1/255
        # interaction surface looks clear while retaining blank-area dragging,
        # edge resizing and the context menu. Image opacity stays untouched.
        color = QColor(0, 0, 0, 1) if mode == 'transparent' else QColor(self.BACKGROUNDS[mode][1])
        self.setBackgroundBrush(color)
        self.setViewportUpdateMode(self.ViewportUpdateMode.FullViewportUpdate if mode == 'transparent'
                                   else self.ViewportUpdateMode.MinimalViewportUpdate)
        self.glow_timer.stop()
        for key, action in getattr(self, 'background_actions', {}).items():
            action.setChecked(key == mode)
        if persist:
            self.catalog.set_setting('board.background', mode)
        self.viewport().update()

    def drawBackground(self, painter, rect):
        painter.save()
        # Replace old alpha as well as RGB; otherwise moved images leave trails.
        painter.setCompositionMode(QPainter.CompositionMode_Source)
        painter.fillRect(rect, self.backgroundBrush())
        painter.restore()

    def glow_active(self):
        return self.background_mode == 'transparent' and (
            self.window_drag is not None or self.window_tool is not None or self.glow_timer.isActive())

    def pulse_edge(self, duration=500):
        if self.background_mode == 'transparent':
            self.glow_timer.start(duration)
            self.viewport().update()

    def drawForeground(self, painter, rect):
        super().drawForeground(painter, rect)
        if not self.glow_active():
            return
        painter.save()
        painter.resetTransform()
        painter.setBrush(Qt.NoBrush)
        painter.setRenderHint(QPainter.Antialiasing)
        bounds = QRectF(self.viewport().rect())
        for inset in range(12, 0, -1):
            color = QColor(115, 216, 255, round(100 * (1 - inset / 13) ** 2))
            painter.setPen(QPen(color, 2))
            painter.drawRoundedRect(bounds.adjusted(inset, inset, -inset, -inset), 3, 3)
        painter.setPen(QPen(QColor(190, 239, 255, 245), 1.5))
        painter.drawRoundedRect(bounds.adjusted(1, 1, -1, -1), 3, 3)
        painter.restore()

    def _action(self, name, callback, shortcut=None):
        action = QAction(name, self)
        action.triggered.connect(lambda _checked=False: callback())
        if shortcut:
            action.setShortcut(QKeySequence(shortcut))
            action.setShortcutContext(Qt.WindowShortcut)
        self.addAction(action)
        self.commands[name] = action
        return action

    def _commands(self):
        self.background_actions = {}
        self.background_group = QActionGroup(self)
        self.background_group.setExclusive(True)
        for mode, (label, _) in self.BACKGROUNDS.items():
            action = self._action(label + '背景', lambda m=mode: self.set_background(m))
            action.setCheckable(True)
            action.setChecked(mode == self.background_mode)
            self.background_group.addAction(action)
            self.background_actions[mode] = action
        for name, mode, key in [('紧凑排列', 'optimal', 'Ctrl+P'), ('按添加顺序', 'addition', 'Ctrl+Alt+A'),
                                ('按名称', 'name', 'Ctrl+Alt+N'), ('按自定义顺序', 'order', 'Ctrl+Alt+O'),
                                ('按来源路径', 'path', 'Ctrl+Alt+D'), ('随机排列', 'random', 'Ctrl+Alt+R')]:
            self._action(name, lambda m=mode: self.arrange(m), key)
        for label, side, key in [('左对齐', 'left', 'Ctrl+Left'), ('右对齐', 'right', 'Ctrl+Right'),
                                 ('顶部对齐', 'top', 'Ctrl+Up'), ('底部对齐', 'bottom', 'Ctrl+Down')]:
            self._action(label, lambda s=side: self.align(s), key)
        self._action('水平等距分布', lambda: self.distribute(True))
        self._action('垂直等距分布', lambda: self.distribute(False))
        self._action('保存当前空间顺序', self.store_order)
        self._action('撤销', lambda: self.undo(-1), 'Ctrl+Z')
        self._action('重做', lambda: self.undo(1), 'Ctrl+Shift+Z')
        self._action('全选', lambda: [p.setSelected(True) for p in self.pictures.values()], 'Ctrl+A')
        self._action('清除选择', self.scene_model.clearSelection)
        self._action('适合窗口', self.fit, 'F')
        self._action('实际大小', self.actual, '1')
        self._action('统一图片宽度…', self.set_width)
        self._action('顺时针旋转 90°', lambda: self.rotate(90), 'R')
        self._action('逆时针旋转 90°', lambda: self.rotate(-90), 'Shift+R')
        self._action('顺时针微调 15°', lambda: self.rotate(15), ']')
        self._action('逆时针微调 15°', lambda: self.rotate(-15), '[')
        self._action('设置旋转角度…', self.rotation_dialog)
        self._action('恢复原始角度', lambda: self.rotate(0, absolute=True), 'Ctrl+R')
        self._action('上一标签／参考组', lambda: self.change_group(-1), 'PgUp')
        self._action('下一标签／参考组', lambda: self.change_group(1), 'PgDown')
        top = self._action('始终置顶', self.toggle_topmost, 'Ctrl+Shift+A')
        top.setCheckable(True)
        self._action('普通层级', lambda: self.set_layer('normal'))
        self._action('总在最前', lambda: self.set_layer('top'))
        self._action('总在最后', lambda: self.set_layer('bottom'), 'Ctrl+Shift+B')
        self._action('在指定应用之上…', self.choose_application, 'Ctrl+Alt+Shift+A')
        self._action('移动窗口', lambda: self.arm_window_tool('move'))
        self._action('调整窗口大小', lambda: self.arm_window_tool('resize'))
        self._action('窗口居中', lambda: self.place_window('center'))
        self._action('窗口左半屏', lambda: self.place_window('left'))
        self._action('窗口右半屏', lambda: self.place_window('right'))
        self._action('设置窗口尺寸…', self.window_size_dialog)
        self._action('最大化／还原', lambda: self.showNormal() if self.isMaximized() else self.showMaximized())
        self._action('全屏／退出全屏', lambda: self.showNormal() if self.isFullScreen() else self.showFullScreen(), 'F11')
        self._action('外观设置…', self.appearance)
        self._action('打开图片库', self.controller.new_library)
        self._action('保存布局', self.save, 'Ctrl+S')
        self._action('命令面板…', self.palette, 'Ctrl+Shift+P')
        self.commands['命令面板…'].setShortcuts([QKeySequence('Ctrl+Shift+P'), QKeySequence('F3')])
        self._action('关闭查看器', self.close, 'Ctrl+Q')
        self._action('退出应用', self.controller.quit)
        self._action('操作帮助', self.help)
        self._action('关于', lambda: QMessageBox.about(self, 'SnipBoard', 'SnipBoard 0.6.3\n标签图片库与作品参考组'))

    def update_groups(self):
        self.groups = self.catalog.collection_rows()
        if self.board_id is not None and not any(g['id'] == self.board_id for g in self.groups):
            self.save_timer.stop()
            self.board_id = None
            self.scene_model.clear()
            self.pictures = {}
            self.undo_states, self.undo_index = [], -1
            self._bounds()
        if self.board_id is None and self.groups:
            remembered = self.catalog.get_setting('board.last_group')
            current = self.catalog.get_setting('work.current')
            if current is not None:
                remembered = f'work:{current}'
            self.load_group(remembered if any(g['id'] == remembered for g in self.groups) else self.groups[0]['id'])
        elif self.board_id is not None:
            rows = self._rows(self.board_id)
            if {r['digest'] for r in rows} != set(self.pictures):
                self.load_group(self.board_id)

    def _rows(self, board_id):
        if isinstance(board_id, str):
            return self.catalog.collection_query(board_id)
        rows = self.catalog.query([board_id])
        return rows or self.catalog.query([board_id], archived=True)

    def load_group(self, board_id):
        self.cancel_arrangement()
        self.save()
        self.board_id = board_id
        self.scene_model.clear()
        self.pictures = {}
        rows = self._rows(board_id)
        saved = self.catalog.layout(board_id)
        legacy = {r['item_id']: dict(r) for r in self.catalog.db.execute('SELECT * FROM positions')}
        positions = saved.get('items', {}) if saved else {}
        max_y = max((v['y'] + v['h'] for v in positions.values()), default=0)
        for index, row in enumerate(rows):
            record = positions.get(row['digest'])
            old = legacy.get(row['id'])
            picture = PictureItem(row, self.images, record['w'] if record else old['width'] if old else 260)
            picture.order = record.get('order', index) if record else index
            picture.setPos(record['x'] if record else old['x'] if old else (index % 4) * 285,
                           record['y'] if record else old['y'] if old else max_y + (index // 4) * 300)
            picture.setZValue(record.get('z', 0) if record else index)
            picture.setRotation(record.get('angle', 0) if record else 0)
            self.scene_model.addItem(picture)
            self.pictures[row['digest']] = picture
        self._bounds()
        self.resetTransform()
        if saved and saved.get('viewport'):
            zoom, x, y = saved['viewport']
            self.scale(zoom, zoom)
            self.centerOn(x, y)
        else:
            self.fit()
            self.arrange('optimal', record=False)
        self.undo_states, self.undo_index = [self._snapshot()], 0
        self.catalog.set_setting('board.last_group', board_id)

    def change_group(self, delta):
        if not self.groups:
            return
        ids = [g['id'] for g in self.groups]
        index = ids.index(self.board_id) if self.board_id in ids else 0
        self.load_group(ids[(index + delta) % len(ids)])

    def _snapshot(self):
        return {key: {'x': p.x(), 'y': p.y(), 'w': p.rect().width(), 'h': p.rect().height(),
                      'z': p.zValue(), 'order': p.order, 'angle': p.rotation()} for key, p in self.pictures.items()}

    def _bounds(self):
        self.scene_model.setSceneRect(self.scene_model.itemsBoundingRect().adjusted(-800, -800, 800, 800))

    def commit(self):
        self.cancel_arrangement()
        state = self._snapshot()
        if self.undo_index >= 0 and state == self.undo_states[self.undo_index]:
            return
        self.undo_states = self.undo_states[:self.undo_index + 1] + [copy.deepcopy(state)]
        self.undo_states = self.undo_states[-80:]
        self.undo_index = len(self.undo_states) - 1
        self._bounds()
        self.save_timer.start(250)

    def undo(self, delta):
        self.cancel_arrangement()
        index = self.undo_index + delta
        if not 0 <= index < len(self.undo_states):
            return
        self.undo_index = index
        for key, record in self.undo_states[index].items():
            picture = self.pictures.get(key)
            if picture:
                picture.setPos(record['x'], record['y'])
                picture.setRect(0, 0, record['w'], record['h'])
                picture.setTransformOriginPoint(picture.rect().center())
                picture.setRotation(record.get('angle', 0))
                picture.setZValue(record['z'])
                picture.order = record['order']
        self._bounds()
        self.save()

    def chosen(self):
        return self.scene_model.selectedItems() or list(self.pictures.values())

    def rotate(self, degrees, absolute=False):
        # Rotation is a layout transform about each image's own centre.
        for picture in self.scene_model.selectedItems():
            picture.setRotation((degrees if absolute else picture.rotation() + degrees) % 360)
        self.commit()

    def rotation_dialog(self):
        selected = self.scene_model.selectedItems()
        if not selected:
            QMessageBox.information(self, '旋转图片', '请先单击选中图片；可多选或 Ctrl+A 全选。')
            return
        angle, ok = QInputDialog.getDouble(self, '旋转图片', '选中图片的角度（顺时针为正）',
                                          selected[0].rotation(), -360, 360, 1)
        if ok:
            self.rotate(angle, absolute=True)

    def arrange(self, mode, record=True):
        self.cancel_arrangement()
        if mode == 'optimal':
            generation = self.arrange_generation
            # Read the viewport after Qt has applied show/resize layout changes.
            QTimer.singleShot(0, lambda: self.compact_arrangement(record)
                              if generation == self.arrange_generation and not self.controller.quitting else None)
            return
        chosen = self.chosen()
        entries = [{'id': p.row['digest'], 'name': p.row['source_name'],
                    'path': str(Path(p.row['source']) / 'history' / p.row['group_id'] / p.row['source_name']),
                    'order': p.order, 'w': p.sceneBoundingRect().width(),
                    'h': p.sceneBoundingRect().height()} for p in chosen]
        # Stable collection IDs, not hashes, define addition order.
        if mode == 'addition':
            entries.sort(key=lambda i: self.pictures[i['id']].row['id'])
            for index, entry in enumerate(entries):
                entry['order'] = index
            mode = 'order'
        positions = arrange(entries, mode, max(self.width(), 1) / max(self.height(), 1), self.gap)
        offset = QPointF(min((p.sceneBoundingRect().left() for p in chosen), default=0),
                         min((p.sceneBoundingRect().top() for p in chosen), default=0))
        for key, (x, y, _, _) in positions.items():
            picture = self.pictures[key]
            picture.setPos(picture.pos() + offset + QPointF(x, y) - picture.sceneBoundingRect().topLeft())
        if record:
            self.commit()
        self._bounds()
        self.fit()

    def cancel_arrangement(self):
        self.arrange_generation += 1
        if self.arrange_cancel is not None:
            self.arrange_cancel.set()
            self.arrange_cancel = None
            if self.window_drag is None and self.window_tool is None:
                self.viewport().unsetCursor()

    def compact_arrangement(self, record=True):
        self.arrange_result = None
        # Fill the current viewport with the entire collection. Selection-based
        # alignment and ordered arrangements retain their existing behavior.
        entries = []
        for picture in self.pictures.values():
            bound = picture.sceneBoundingRect()
            entries.append(dict(id=picture.row['digest'], w=bound.width(), h=bound.height(),
                fill=picture.rect().width()*picture.rect().height()/(bound.width()*bound.height()),
                name=picture.row['source_name'], path='', order=picture.order))
        if not entries:
            return
        inset = 2 * self.COMPACT_MARGIN
        width, height = max(1, self.viewport().width()-inset), max(1, self.viewport().height()-inset)
        baseline = self._snapshot()
        generation, group_id = self.arrange_generation, self.board_id
        cancel = self.arrange_cancel = threading.Event()
        gap = self.compact_gap
        self.viewport().setCursor(Qt.WaitCursor)
        self.controller.status.emit('正在按查看器窗口尺寸计算紧凑排列…')
        def completed(result):
            if (cancel.is_set() or generation != self.arrange_generation or self.controller.quitting or
                    group_id != self.board_id or baseline != self._snapshot() or
                    (width,height) != (self.viewport().width()-inset,self.viewport().height()-inset)):
                if generation == self.arrange_generation:
                    self.cancel_arrangement()
                return
            self.arrange_cancel = None
            self.viewport().unsetCursor()
            self.arrange_result = result
            if result['positions'] is None:
                if not result.get('cancelled'):
                    message = result.get('reason', '当前窗口下无法排列。') + '\n原布局已保留，可在右键“外观设置…”调整间距。'
                    self.controller.status.emit(message)
                    if record:
                        QMessageBox.information(self, '紧凑排列未完成', message)
                return
            for key,(x,y,w,h) in result['positions'].items():
                picture = self.pictures[key]
                factor = w / picture.sceneBoundingRect().width()
                picture.set_width(picture.rect().width()*factor)
                picture.setPos(picture.pos()+QPointF(x,y)-picture.sceneBoundingRect().topLeft())
            self._bounds()
            self.resetTransform()
            self.centerOn(width/2, height/2)
            if record:
                self.commit()
            else:
                self.undo_states, self.undo_index = [self._snapshot()], 0
                self.save_timer.start(250)
            self.controller.status.emit(f"紧凑排列完成 · 图片占用可用窗口 {result['occupancy']:.1%}")
        def failed(error):
            if generation == self.arrange_generation and not self.controller.quitting:
                self.cancel_arrangement()
                self.controller.status.emit('紧凑排列失败，原布局已保留：' + str(error))
        self.controller.jobs.submit(lambda: compact(entries,width,height,gap,cancelled=cancel.is_set),
                                    completed, failed, owner=self, key='compact-layout')

    def align(self, side):
        chosen = self.chosen()
        if not chosen:
            return
        bounds = [p.sceneBoundingRect() for p in chosen]
        edge = min(getattr(b, side)() for b in bounds) if side in ('left', 'top') else max(getattr(b, side)() for b in bounds)
        for picture, bound in zip(chosen, bounds):
            movement = edge - getattr(bound, side)()
            picture.moveBy(movement if side in ('left', 'right') else 0,
                           movement if side in ('top', 'bottom') else 0)
        self.commit()

    def distribute(self, horizontal):
        chosen = sorted(self.chosen(), key=lambda p: p.sceneBoundingRect().left() if horizontal else p.sceneBoundingRect().top())
        if len(chosen) < 3:
            return
        start = chosen[0].sceneBoundingRect().left() if horizontal else chosen[0].sceneBoundingRect().top()
        end = chosen[-1].sceneBoundingRect().right() if horizontal else chosen[-1].sceneBoundingRect().bottom()
        size = lambda p: p.sceneBoundingRect().width() if horizontal else p.sceneBoundingRect().height()
        gap = max(0., (end - start - sum(size(p) for p in chosen)) / (len(chosen) - 1))
        for picture in chosen:
            bound = picture.sceneBoundingRect()
            picture.moveBy(start - bound.left() if horizontal else 0, 0 if horizontal else start - bound.top())
            start += size(picture) + gap
        self.commit()

    def store_order(self):
        for index, picture in enumerate(sorted(self.pictures.values(), key=lambda p: (p.y(), p.x()))):
            picture.order = index
        self.commit()

    def set_width(self):
        width, ok = QInputDialog.getInt(self, '图片大小', '选中图片宽度（未选中时应用于全部）', 260, 40, 4000)
        if ok:
            for picture in self.chosen():
                picture.set_width(width)
            self.commit()

    def fit(self):
        self.cancel_arrangement()
        bounds = self.scene_model.itemsBoundingRect()
        if not bounds.isEmpty():
            self.fitInView(bounds.adjusted(-15, -15, 15, 15), Qt.KeepAspectRatio)

    def actual(self):
        self.cancel_arrangement()
        selected = self.scene_model.selectedItems()
        if selected:
            picture = selected[0]
            ratio = picture.row['width'] / picture.rect().width()
            self.resetTransform()
            self.scale(ratio, ratio)
            self.centerOn(picture)
        else:
            self.resetTransform()

    def appearance(self):
        dialog = QDialog(self)
        dialog.setWindowTitle('排列间距')
        layout = QFormLayout(dialog)
        compact_gap = QSpinBox(dialog)
        compact_gap.setObjectName('compactGap')
        compact_gap.setRange(0, 50)
        compact_gap.setSuffix(' 像素')
        compact_gap.setValue(self.compact_gap)
        compact_gap.setToolTip('紧凑排列中的最小间距；0 为相接，默认 3。保存后立即重新排列。')
        ordered_gap = QSpinBox(dialog)
        ordered_gap.setObjectName('orderedGap')
        ordered_gap.setRange(0, 150)
        ordered_gap.setValue(self.gap)
        layout.addRow('紧凑排列间距', compact_gap)
        layout.addRow('其他排列间距', ordered_gap)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, parent=dialog)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addRow(buttons)
        if dialog.exec() == QDialog.Accepted:
            self.cancel_arrangement()
            changed = self.compact_gap != compact_gap.value()
            self.compact_gap, self.gap = compact_gap.value(), ordered_gap.value()
            self.catalog.set_setting('board.compact_gap', self.compact_gap)
            self.catalog.set_setting('board.gap', self.gap)
            if changed and self.pictures:
                self.arrange('optimal')
        dialog.deleteLater()

    def toggle_topmost(self):
        self.set_layer('normal' if self.layer.mode == 'top' else 'top')

    def sync_layer_action(self):
        self.commands['始终置顶'].setChecked(self.layer.mode == 'top')

    def set_layer(self, mode):
        self.layer.set_mode(mode)
        self.catalog.set_setting('board.layer', mode if mode != 'application' else 'normal')

    def choose_application(self):
        windows = self.layer.windows()
        if not windows:
            QMessageBox.information(self, '指定应用', '没有可选择的应用窗口。')
            return
        labels = [f'{title}  [{handle}]' for handle, title in windows]
        selected, ok = QInputDialog.getItem(self, '在指定应用之上', '选择目标应用（仅在该应用处于前台时置顶）', labels, 0, False)
        if ok:
            self.layer.set_mode('application', windows[labels.index(selected)][0])

    def palette(self):
        names = [name for name in self.commands if name != '命令面板…']
        selected, ok = QInputDialog.getItem(self, '命令面板', '选择或输入命令', names, 0, True)
        if ok and selected in self.commands:
            self.commands[selected].trigger()

    def help(self):
        QMessageBox.information(self, '查看器操作',
            'Ctrl+P：按当前窗口紧凑排列全部图片，分别等比缩放并兼顾大小均衡。\n'
            '移动窗口：拖动背景空白处，或 Alt＋左键拖动任意位置。\n'
            '右键“背景”：黑色、白色、灰色、透明；透明时移动和缩放窗口会显示发光边缘。\n'
            '调整窗口：拖动边缘／角落，或 Alt＋右键拖动任意位置。\n'
            '右键菜单的移动／调整命令：选择后左键拖动，Esc 取消。\n'
            '双击空白处：最大化／还原；右键窗口菜单可居中、半屏、设置尺寸。\n'
            '选择图片：单击；Ctrl＋单击多选；Shift＋拖动空白处框选。\n'
            '悬停图片右下角：旋转光标，按住拖动旋转；Shift 吸附到 15°。\n'
            '选中图片左下角：拖动调整图片尺寸。\n'
            '旋转选中图片：R／Shift+R 转 90°；[／] 微调 15°；Ctrl+R 复位。\n'
            'Shift＋滚轮旋转 15°，Ctrl＋Shift＋滚轮精调 1°。\n'
            '滚轮缩放画布；空格＋左键或中键拖动平移；F 适合窗口。')

    def arm_window_tool(self, tool):
        self.window_tool = tool
        self.pulse_edge()
        self.viewport().setCursor(Qt.SizeAllCursor if tool == 'move' else Qt.SizeFDiagCursor)

    def edge_at(self, point):
        edges = Qt.Edges()
        if not self.isMaximized() and not self.isFullScreen():
            if point.x() < 10: edges |= Qt.LeftEdge
            if point.x() >= self.viewport().width() - 10: edges |= Qt.RightEdge
            if point.y() < 10: edges |= Qt.TopEdge
            if point.y() >= self.viewport().height() - 10: edges |= Qt.BottomEdge
        return edges

    @staticmethod
    def edge_cursor(edges):
        horizontal = bool(edges & (Qt.LeftEdge | Qt.RightEdge))
        vertical = bool(edges & (Qt.TopEdge | Qt.BottomEdge))
        if horizontal and vertical:
            return Qt.SizeFDiagCursor if bool(edges & Qt.LeftEdge) == bool(edges & Qt.TopEdge) else Qt.SizeBDiagCursor
        return Qt.SizeHorCursor if horizontal else Qt.SizeVerCursor

    def begin_window_drag(self, global_pos, edges=Qt.Edges()):
        if self.isFullScreen() or self.isMaximized():
            return False
        self.window_drag = (global_pos.toPoint(), QRect(self.geometry()), edges)
        self.pulse_edge()
        self.viewport().setCursor(self.edge_cursor(edges) if edges else Qt.SizeAllCursor)
        return True

    def drag_window(self, global_pos):
        origin, original, edges = self.window_drag
        delta = global_pos.toPoint() - origin
        rect = QRect(original)
        if not edges:
            rect.translate(delta)
        else:
            if edges & Qt.LeftEdge: rect.setLeft(min(original.left() + delta.x(), original.right() - self.minimumWidth() + 1))
            if edges & Qt.RightEdge: rect.setRight(max(original.right() + delta.x(), original.left() + self.minimumWidth() - 1))
            if edges & Qt.TopEdge: rect.setTop(min(original.top() + delta.y(), original.bottom() - self.minimumHeight() + 1))
            if edges & Qt.BottomEdge: rect.setBottom(max(original.bottom() + delta.y(), original.top() + self.minimumHeight() - 1))
        self.setGeometry(rect)

    def place_window(self, mode):
        area = self.screen().availableGeometry()
        if self.isFullScreen() or self.isMaximized():
            self.showNormal()
        if mode == 'center':
            rect = QRect(0, 0, min(self.width(), area.width()), min(self.height(), area.height()))
            rect.moveCenter(area.center())
        else:
            half = area.width() // 2
            rect = QRect(area.x() if mode == 'left' else area.x() + half, area.y(),
                         half if mode == 'left' else area.width() - half, area.height())
        self.setGeometry(rect)
        self.save_timer.start(250)

    def window_size_dialog(self):
        width, ok = QInputDialog.getInt(self, '窗口尺寸', '宽度（逻辑像素）', self.width(), self.minimumWidth(), 10000)
        if not ok:
            return
        height, ok = QInputDialog.getInt(self, '窗口尺寸', '高度（逻辑像素）', self.height(), self.minimumHeight(), 10000)
        if ok:
            self.showNormal()
            self.resize(width, height)
            self.place_window('center')

    def focusOutEvent(self, event):
        self.space = False
        self.pan_start = None
        self.window_drag = None
        self.window_tool = None
        self.glow_timer.stop()
        self.viewport().update()
        self.viewport().unsetCursor()
        super().focusOutEvent(event)

    def contextMenuEvent(self, event):
        if self.suppress_context:
            self.suppress_context = False
            event.accept()
            return
        hit = self.itemAt(event.pos())
        if isinstance(hit, PictureItem) and not hit.isSelected():
            self.scene_model.clearSelection()
            hit.setSelected(True)
        menu = QMenu(self)
        menu.addAction(self.commands['始终置顶'])
        menu.addSeparator()
        background = menu.addMenu('背景')
        for action in self.background_actions.values():
            background.addAction(action)
        if isinstance(hit, PictureItem):
            digests = [p.row['digest'] for p in self.scene_model.selectedItems()]
            title = f'从图片库删除所选 {len(digests)} 张图片（保留源文件）' if len(digests) > 1 else '从图片库删除图片（保留源文件）'
            menu.addAction(title, lambda: self.controller.delete_images(digests))
            menu.addSeparator()
        groups = menu.addMenu('标签与参考组')
        for group in self.groups:
            prefix = {'work': '参考组', 'source': 'Snipaste', 'local': '本地', 'manual': '标签'}[group['kind']]
            action = groups.addAction(f"{prefix} · {group['name']}")
            action.setCheckable(True)
            action.setChecked(group['id'] == self.board_id)
            action.triggered.connect(lambda checked=False, ident=group['id']: self.load_group(ident))
        groups.addSeparator()
        groups.addAction(self.commands['上一标签／参考组'])
        groups.addAction(self.commands['下一标签／参考组'])
        for label, names in [
            ('排列', ['紧凑排列','按添加顺序','按名称','按自定义顺序','按来源路径','随机排列','保存当前空间顺序']),
            ('对齐与分布', ['左对齐','右对齐','顶部对齐','底部对齐','水平等距分布','垂直等距分布']),
            ('图片与视口', ['全选','清除选择','统一图片宽度…','适合窗口','实际大小']),
            ('旋转选中图片', ['顺时针旋转 90°','逆时针旋转 90°','顺时针微调 15°','逆时针微调 15°','设置旋转角度…','恢复原始角度']),
            ('窗口', ['普通层级','总在最前','总在最后','在指定应用之上…','移动窗口','调整窗口大小','窗口居中','窗口左半屏','窗口右半屏','设置窗口尺寸…','最大化／还原','全屏／退出全屏'])]:
            submenu = menu.addMenu(label)
            if label == '旋转选中图片':
                submenu.setEnabled(bool(self.scene_model.selectedItems()))
            for name in names:
                submenu.addAction(self.commands[name])
        menu.addSeparator()
        for name in ['撤销','重做','保存布局','外观设置…','命令面板…','打开图片库','操作帮助','关于','关闭查看器','退出应用']:
            menu.addAction(self.commands[name])
        menu.exec(event.globalPos())

    def wheelEvent(self, event):
        self.cancel_arrangement()
        if event.modifiers() & Qt.ShiftModifier:
            step = 1 if event.modifiers() & Qt.ControlModifier else 15
            self.rotate(step * event.angleDelta().y() / 120)
            event.accept()
            return
        factor = 1.15 ** (event.angleDelta().y() / 120)
        if .02 <= self.transform().m11() * factor <= 30:
            self.scale(factor, factor)
            self.save_timer.start(400)
        event.accept()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Space:
            self.space = True
            self.setCursor(Qt.OpenHandCursor)
            event.accept()
        elif event.key() == Qt.Key_Escape:
            self.cancel_arrangement()
            if self.window_drag:
                self.setGeometry(self.window_drag[1])
                self.window_drag = None
            self.window_tool = None
            self.glow_timer.stop()
            self.viewport().update()
            self.viewport().unsetCursor()
            self.scene_model.clearSelection()
            event.accept()
        else:
            super().keyPressEvent(event)

    def keyReleaseEvent(self, event):
        if event.key() == Qt.Key_Space:
            self.space = False
            self.unsetCursor()
        super().keyReleaseEvent(event)

    def mousePressEvent(self, event):
        self.cancel_arrangement()
        pos = event.position()
        self.suppress_context = False
        if event.button() == Qt.RightButton and event.modifiers() & Qt.AltModifier:
            self.suppress_context = True
            edges = (Qt.LeftEdge if pos.x() < self.width() / 2 else Qt.RightEdge) | (
                     Qt.TopEdge if pos.y() < self.height() / 2 else Qt.BottomEdge)
            self.begin_window_drag(event.globalPosition(), edges)
            event.accept()
            return
        if event.button() == Qt.LeftButton:
            if self.window_tool:
                self.begin_window_drag(event.globalPosition(),
                    Qt.RightEdge | Qt.BottomEdge if self.window_tool == 'resize' else Qt.Edges())
                self.window_tool = None
                event.accept()
                return
            edges = self.edge_at(pos)
            if event.modifiers() & Qt.AltModifier or edges:
                self.begin_window_drag(event.globalPosition(), Qt.Edges() if event.modifiers() & Qt.AltModifier else edges)
                event.accept()
                return
        if event.button() == Qt.MiddleButton or (event.button() == Qt.LeftButton and self.space):
            self.pan_start = event.position()
            self.setCursor(Qt.ClosedHandCursor)
            return
        if event.button() == Qt.LeftButton and not event.modifiers() and self.itemAt(pos.toPoint()) is None:
            if self.begin_window_drag(event.globalPosition()):
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.window_drag:
            self.drag_window(event.globalPosition())
            event.accept()
            return
        if self.pan_start is not None:
            delta = event.position() - self.pan_start
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - round(delta.x()))
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - round(delta.y()))
            self.pan_start = event.position()
            return
        super().mouseMoveEvent(event)
        if not event.buttons() and not self.space and not self.window_tool:
            edges = self.edge_at(event.position())
            if edges:
                self.viewport().setCursor(self.edge_cursor(edges))
            elif self.itemAt(event.position().toPoint()) is None:
                self.viewport().setCursor(Qt.SizeAllCursor if not (self.isMaximized() or self.isFullScreen()) else Qt.ArrowCursor)
            else:
                hit = self.itemAt(event.position().toPoint())
                if isinstance(hit, PictureItem):
                    hit.update_hover(hit.mapFromScene(self.mapToScene(event.position().toPoint())))
                    self.viewport().setCursor(hit.cursor())
                else:
                    self.viewport().unsetCursor()

    def mouseReleaseEvent(self, event):
        if self.window_drag:
            self.drag_window(event.globalPosition())
            self.window_drag = None
            self.pulse_edge(300)
            self.viewport().unsetCursor()
            self.save_timer.start(250)
            event.accept()
            return
        if self.pan_start is not None:
            self.pan_start = None
            self.unsetCursor()
            self.save_timer.start(250)
            return
        super().mouseReleaseEvent(event)
        self.commit()

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton and self.itemAt(event.position().toPoint()) is None and not self.isFullScreen():
            self.window_drag = None
            self.showNormal() if self.isMaximized() else self.showMaximized()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def moveEvent(self, event):
        super().moveEvent(event)
        if hasattr(self, 'save_timer'):
            self.save_timer.start(400)
            self.pulse_edge()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, 'save_timer'):
            self.save_timer.start(400)
            self.pulse_edge()

    def save(self):
        if self.board_id is not None:
            center = self.mapToScene(self.viewport().rect().center())
            self.catalog.save_layout(self.board_id, {'items': self._snapshot(),
                                    'viewport': [self.transform().m11(), center.x(), center.y()]})
        if not self.isMaximized() and not self.isFullScreen():
            rect = self.geometry()
            self.catalog.set_setting('board.geometry', [rect.x(), rect.y(), rect.width(), rect.height()])

    def closeEvent(self, event):
        self.cancel_arrangement()
        self.window_drag = self.window_tool = None
        self.glow_timer.stop()
        self.save_timer.stop()
        self.save()
        self.hide()
        event.ignore()
