"""Independent library windows with inline detail and two recommendation rows."""
from __future__ import annotations

import base64
import binascii

from pathlib import Path
from functools import lru_cache

from PySide6.QtCore import Qt, QSize, QRectF, QAbstractListModel, QModelIndex, QTimer, Signal, QEvent
from PySide6.QtGui import QAction, QColor, QPainter, QPen, QPixmap, QKeySequence, QGuiApplication, QFont, QBrush
from PySide6.QtWidgets import (QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QPushButton, QListView, QListWidget, QListWidgetItem, QStyledItemDelegate,
    QLineEdit, QComboBox, QSlider, QCheckBox, QSplitter, QStackedWidget, QToolBar,
    QGraphicsView, QGraphicsScene, QFileDialog, QMessageBox, QColorDialog, QInputDialog, QStyle, QScrollArea, QMenu, QDockWidget)

from .catalog import Catalog
from .features import COLORS
from .qt_filters import FilterPanel
from .qt_collections import PhotoGrid, WorkGroupList, NewGroupDrop


@lru_cache(maxsize=1)
def transparency_brush():
    tile = QPixmap(16, 16)
    tile.fill(QColor('#e1e3e6'))
    painter = QPainter(tile)
    painter.fillRect(0, 0, 8, 8, QColor('#c2c6cb'))
    painter.fillRect(8, 8, 8, 8, QColor('#c2c6cb'))
    painter.end()
    return QBrush(tile)


class GroupList(WorkGroupList):
    textClicked = Signal(object)

    def __init__(self):
        super().__init__(allow_empty=False)

    def mousePressEvent(self, event):
        item = self.itemAt(event.position().toPoint())
        if item and event.button() == Qt.LeftButton and event.position().x() > 30:
            self.setCurrentItem(item)
            self.textClicked.emit(item.data(Qt.UserRole))
            event.accept()
            return
        super().mousePressEvent(event)


class PhotoModel(QAbstractListModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows = []

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def flags(self, index):
        return super().flags(index) | Qt.ItemIsDragEnabled if index.isValid() else super().flags(index)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid() or index.row() >= len(self.rows):
            return None
        row = self.rows[index.row()]
        if role == Qt.UserRole:
            return row
        if role == Qt.ToolTipRole:
            return f"{row['source_name']}\n{row['width']} × {row['height']}\n" + ' / '.join(name for _, name in row['memberships'])
        return None

    def replace(self, rows):
        self.beginResetModel()
        self.rows = rows
        self.endResetModel()


class PhotoDelegate(QStyledItemDelegate):
    def __init__(self, images, compact=False, parent=None):
        super().__init__(parent)
        self.images, self.compact, self.size = images, compact, 205

    def sizeHint(self, option, index):
        return QSize(128, 96) if self.compact else QSize(self.size, self.size + 24)

    def paint(self, painter, option, index):
        row = index.data(Qt.UserRole)
        if not row:
            return
        painter.save()
        rect = QRectF(option.rect).adjusted(6, 6, -6, -6)
        selected = bool(option.state & QStyle.State_Selected)
        painter.setPen(QPen(QColor('#87bccc') if selected else QColor('#343b44'), 1))
        painter.setBrush(QColor('#2a3037') if selected else QColor('#23272e'))
        painter.drawRoundedRect(rect, 7, 7)
        picture_rect = rect.adjusted(8, 8, -8, -8 if self.compact else -31)
        image = self.images.request(row['thumbnail'], 320)
        if image:
            size = image.size().scaled(picture_rect.size().toSize(), Qt.KeepAspectRatio)
            target = QRectF(0, 0, size.width(), size.height())
            target.moveCenter(picture_rect.center())
            painter.setRenderHint(QPainter.SmoothPixmapTransform)
            if image.hasAlphaChannel():
                painter.fillRect(target, transparency_brush())
            painter.drawPixmap(target, image, QRectF(image.rect()))
        else:
            painter.fillRect(picture_rect, QColor('#2c323a'))
        if not self.compact:
            painter.setPen(QColor('#b4becb'))
            caption = row['board_name']
            if len(row['memberships']) > 1:
                caption += f"  +{len(row['memberships']) - 1}"
            caption = painter.fontMetrics().elidedText(caption, Qt.ElideRight, int(rect.width() - 20))
            painter.drawText(rect.adjusted(10, rect.height() - 27, -10, -3), Qt.AlignVCenter, caption)
        painter.restore()


class DetailView(QGraphicsView):
    def __init__(self, images, parent=None):
        self.scene_model = QGraphicsScene()
        super().__init__(self.scene_model, parent)
        self.images, self.row, self.image_item, self.fitted = images, None, None, True
        self.setBackgroundBrush(QColor('#121417'))
        self.setFrameShape(self.Shape.NoFrame)
        self.setDragMode(self.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(self.ViewportAnchor.AnchorUnderMouse)
        self.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        images.ready.connect(self._ready)

    def display(self, row):
        self.row, self.fitted = row, True
        self.scene_model.clear()
        self.image_item = None
        self._refresh()

    def _ready(self, path):
        if self.row and path in (self.row['asset'], self.row['thumbnail']):
            self._refresh()

    def _refresh(self):
        if not self.row:
            return
        picture = self.images.request(self.row['asset'], 4096)
        if picture is None:
            picture = self.images.request(self.row['thumbnail'], 800)
        if picture:
            first = self.image_item is None
            if first:
                self.image_item = self.scene_model.addPixmap(picture)
            else:
                self.image_item.setPixmap(picture)
            self.image_item.setScale(self.row['width'] / picture.width())
            self.scene_model.setSceneRect(self.image_item.sceneBoundingRect())
            if self.fitted:
                self.fit()

    def fit(self):
        self.fitted = True
        if self.image_item:
            self.fitInView(self.image_item.sceneBoundingRect().adjusted(-5, -5, 5, 5), Qt.KeepAspectRatio)

    def actual(self):
        self.fitted = False
        self.resetTransform()

    def drawBackground(self, painter, rect):
        super().drawBackground(painter, rect)
        if self.image_item and self.image_item.pixmap().hasAlphaChannel():
            painter.fillRect(self.image_item.sceneBoundingRect(), transparency_brush())

    def wheelEvent(self, event):
        factor = 1.15 ** (event.angleDelta().y() / 120)
        if .01 <= self.transform().m11() * factor <= 16:
            self.fitted = False
            self.scale(factor, factor)
        event.accept()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.fitted:
            self.fit()


class LibraryWindow(QMainWindow):
    def __init__(self, controller, state=None):
        super().__init__()
        self.controller, self.catalog, self.images = controller, controller.catalog, controller.images
        self.rows, self.current, self.recommended = [], None, []
        self.selected_groups = None
        self.selected_work = None
        self.colors = []
        self.query_generation = self.rec_generation = 0
        self.nav, self.nav_index, self.grid_position = [], -1, 0
        self.restoring = False
        self.pending_restore = None
        self.resize(1500, 950)
        self.setMinimumSize(1080, 700)
        self.setWindowTitle('SnipBoard · 图片库')
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.query_timer = QTimer(self)
        self.query_timer.setSingleShot(True)
        self.query_timer.timeout.connect(self.refresh)
        self._build()
        self._menus()
        self.update_groups()
        if state:
            self.restore_state(state)
        self.images.ready.connect(self._repaint_images)
        controller.changed.connect(self.source_changed)
        controller.status.connect(self.statusBar().showMessage)
        self.statusBar().showMessage('贴图图组 · 本地图片库')
        self.refresh()

    def _build(self):
        toolbar = QToolBar(self)
        toolbar.setObjectName("library.toolbar")
        toolbar.setMovable(False)
        self.addToolBar(toolbar)
        for label, callback in [('← 返回', self.back), ('前进 →', self.forward)]:
            button = QPushButton(label)
            button.clicked.connect(callback)
            toolbar.addWidget(button)
        toolbar.addSeparator()
        self.search = QLineEdit()
        self.search.setPlaceholderText('搜索文件名、备注或标签')
        self.search.setClearButtonEnabled(True)
        self.search.setMinimumWidth(230)
        self.search.textChanged.connect(lambda _: self.query_timer.start(160))
        toolbar.addWidget(self.search)
        self.sort = QComboBox()
        self.sort.addItems(['最近加入', '最早加入', '文件名称', '图片宽度'])
        self.sort.currentIndexChanged.connect(lambda _: self.refresh())
        toolbar.addWidget(self.sort)
        self.thumb_size = QSlider(Qt.Horizontal)
        self.thumb_size.setRange(135, 300)
        self.thumb_size.setValue(205)
        self.thumb_size.setFixedWidth(110)
        self.thumb_size.setToolTip('缩略图大小')
        self.thumb_size.valueChanged.connect(self.resize_thumbnails)
        toolbar.addWidget(self.thumb_size)
        for label, callback in [('刷新', self.controller.sync), ('参考图查看器', self.open_board)]:
            button = QPushButton(label)
            button.clicked.connect(callback)
            toolbar.addWidget(button)

        self.docks = {}
        self.setDockOptions(QMainWindow.AnimatedDocks | QMainWindow.AllowNestedDocks |
                            QMainWindow.AllowTabbedDocks | QMainWindow.GroupedDragging)
        self.setCorner(Qt.BottomLeftCorner, Qt.LeftDockWidgetArea)
        self.setCorner(Qt.BottomRightCorner, Qt.RightDockWidgetArea)
        self.filter_panel = FilterPanel()
        self.filter_panel.setParent(self)
        self.filter_panel.hide()
        self.filter_panel.changed.connect(self.filters_changed)
        self.filter_panel.clearRequested.connect(self.clear_filters)
        self.tolerance, self.archived = self.filter_panel.tolerance, self.filter_panel.archived
        self.color_buttons = {}
        titles = {'color': '色彩筛选', 'brightness': '明度', 'aspect': '图片比例',
                  'dimensions': '图片尺寸', 'filter_tools': '筛选管理'}
        for key, widget in self.filter_panel.sections.items():
            self.add_panel(key, titles[key], widget, Qt.LeftDockWidgetArea, scroll=True)

        center = QWidget()
        layout = QVBoxLayout(center)
        layout.setContentsMargins(18, 16, 18, 12)
        self.heading = QLabel('全部图片')
        self.heading.setObjectName('title')
        layout.addWidget(self.heading)
        self.summary = QLabel('正在读取图片库…')
        self.summary.setStyleSheet('color:#929eaa;')
        layout.addWidget(self.summary)
        self.stack = QStackedWidget()
        layout.addWidget(self.stack, 1)
        self.grid = PhotoGrid()
        self.grid.setViewMode(QListView.IconMode)
        self.grid.setResizeMode(QListView.Adjust)
        self.grid.setMovement(QListView.Static)
        self.grid.setUniformItemSizes(True)
        self.grid.setLayoutMode(QListView.Batched)
        self.grid.setBatchSize(80)
        self.grid.setSpacing(0)
        self.grid.setStyleSheet('QListView { background:#17191d; border:none; padding:0; }')
        self.grid.setVerticalScrollMode(QListView.ScrollPerPixel)
        self.model = PhotoModel(self)
        self.delegate = PhotoDelegate(self.images, parent=self.grid)
        self.grid.setModel(self.model)
        self.grid.setItemDelegate(self.delegate)
        self.grid.setContextMenuPolicy(Qt.CustomContextMenu)
        self.grid.customContextMenuRequested.connect(lambda pos: self.photo_menu(self.grid, pos))
        self.grid.clicked.connect(lambda index: self.show_photo(index.data(Qt.UserRole))
            if not QGuiApplication.keyboardModifiers() & (Qt.ControlModifier | Qt.ShiftModifier) else None)
        self.stack.addWidget(self.grid)

        detail = QWidget()
        detail_layout = QVBoxLayout(detail)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        self.detail_view = DetailView(self.images)
        self.detail_view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.detail_view.customContextMenuRequested.connect(self.detail_menu)
        detail_layout.addWidget(self.detail_view, 1)
        controls = QHBoxLayout()
        for label, callback in [('返回网格', self.return_to_grid), ('适合窗口', self.detail_view.fit),
                                ('实际大小', self.detail_view.actual), ('备注与标签…', self.annotate)]:
            button = QPushButton(label)
            button.clicked.connect(callback)
            controls.addWidget(button)
        controls.addStretch()
        detail_layout.addLayout(controls)
        tag_controls = QHBoxLayout()
        tag_controls.addWidget(QLabel('手动标签'))
        self.tag_editor = QLineEdit()
        self.tag_editor.setPlaceholderText('人物，衣褶，暖光…（逗号分隔）')
        self.tag_editor.returnPressed.connect(self.save_tags)
        tag_controls.addWidget(self.tag_editor, 1)
        save_tags = QPushButton('保存标签')
        save_tags.clicked.connect(self.save_tags)
        tag_controls.addWidget(save_tags)
        add_current = QPushButton('加入当前工作组')
        add_current.clicked.connect(self.add_current_photo)
        tag_controls.addWidget(add_current)
        detail_layout.addLayout(tag_controls)
        self.source_tags = QLabel()
        self.source_tags.setWordWrap(True)
        self.source_tags.setStyleSheet('color:#8d9aa8;font-size:11px;')
        detail_layout.addWidget(self.source_tags)
        recommendations = QWidget()
        recommendation_layout = QVBoxLayout(recommendations)
        recommendation_layout.setContentsMargins(8, 8, 8, 8)
        recommendation_heading = QHBoxLayout()
        recommendation_heading.addWidget(QLabel('相似图片 · 色彩与构图'))
        recommendation_heading.addStretch()
        self.rec_scope = QComboBox()
        self.rec_scope.addItems(['当前交叉筛选范围', '标签与参考组，不限图像属性', '全部图片'])
        self.rec_scope.currentIndexChanged.connect(lambda _: self.request_recommendations())
        recommendation_heading.addWidget(self.rec_scope)
        recommendation_layout.addLayout(recommendation_heading)
        self.rec_views, self.rec_models = [], []
        for _ in range(2):
            view = PhotoGrid()
            view.setViewMode(QListView.IconMode)
            view.setFlow(QListView.LeftToRight)
            view.setWrapping(False)
            view.setMovement(QListView.Static)
            view.setUniformItemSizes(True)
            view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            view.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            view.setFixedHeight(102)
            model = PhotoModel(view)
            view.setModel(model)
            view.setItemDelegate(PhotoDelegate(self.images, compact=True, parent=view))
            view.setContextMenuPolicy(Qt.CustomContextMenu)
            view.customContextMenuRequested.connect(lambda pos, grid=view: self.photo_menu(grid, pos))
            view.clicked.connect(lambda index: self.show_photo(index.data(Qt.UserRole)))
            recommendation_layout.addWidget(view)
            self.rec_views.append(view)
            self.rec_models.append(model)
        self.rec_reflow_timer = QTimer(self)
        self.rec_reflow_timer.setSingleShot(True)
        self.rec_reflow_timer.timeout.connect(self.reflow_recommendations)
        self.rec_views[0].viewport().installEventFilter(self)
        self.rec_status = QLabel('选择一张图片后显示相似推荐')
        self.rec_status.setStyleSheet('color:#87929f; font-size:11px;')
        recommendation_layout.addWidget(self.rec_status)
        self.stack.addWidget(detail)
        self.setCentralWidget(center)
        self.add_panel('recommendations', '相似推荐', recommendations, Qt.BottomDockWidgetArea)
        work_panel = QWidget()
        work_layout = QVBoxLayout(work_panel)
        self.current_work_label = QLabel('尚未设置当前工作组')
        self.current_work_label.setWordWrap(True)
        self.current_work_label.setStyleSheet('color:#a3d7cf;')
        work_layout.addWidget(self.current_work_label)
        self.new_group_drop = NewGroupDrop()
        self.new_group_drop.imagesDropped.connect(self.drop_to_work)
        work_layout.addWidget(self.new_group_drop)
        self.work_list = WorkGroupList()
        self.work_list.imagesDropped.connect(self.drop_to_work)
        self.work_list.itemClicked.connect(lambda item: self.select_work(item.data(Qt.UserRole)))
        self.work_list.itemDoubleClicked.connect(lambda _: self.rename_work())
        self.work_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.work_list.customContextMenuRequested.connect(self.work_menu)
        work_layout.addWidget(self.work_list, 1)
        buttons = QHBoxLayout()
        for label, callback in [('新建', self.create_work), ('重命名', self.rename_work), ('★ 当前', self.activate_work)]:
            button = QPushButton(label)
            button.clicked.connect(callback)
            buttons.addWidget(button)
        work_layout.addLayout(buttons)
        clear_work = QPushButton('不限参考组（保留其他筛选）')
        clear_work.clicked.connect(lambda: self.select_work(None))
        work_layout.addWidget(clear_work)
        self.add_panel('work', '当前工作组与参考组', work_panel, Qt.RightDockWidgetArea)
        label_panel = QWidget()
        label_layout = QVBoxLayout(label_panel)
        self.label_mode = QComboBox()
        self.label_mode.addItems(['满足所有所选标签（交集）', '满足任一所选标签（并集）'])
        self.label_mode.currentIndexChanged.connect(lambda _: self.refresh())
        label_layout.addWidget(self.label_mode)
        self.groups = GroupList()
        self.groups.setStyleSheet('QListWidget::item { padding: 4px 2px; }')
        self.groups.setToolTip('拖入缩略图即可加入已有标签（支持多选）；Snipaste 标签的手动归类仅保存在本应用。勾选可交叉筛选；单击名称只选择此标签。')
        self.groups.imagesDropped.connect(self.drop_to_label)
        self.groups.textClicked.connect(self.select_group)
        self.groups.itemChanged.connect(self.groups_changed)
        self.groups.setContextMenuPolicy(Qt.CustomContextMenu)
        self.groups.customContextMenuRequested.connect(self.label_menu)
        label_layout.addWidget(self.groups, 1)
        self.all_button = QPushButton('不限标签（保留其他筛选）')
        self.all_button.clicked.connect(self.select_all)
        label_layout.addWidget(self.all_button)
        self.add_panel('labels', '标签', label_panel, Qt.RightDockWidgetArea)
        self.splitDockWidget(self.docks['color'], self.docks['aspect'], Qt.Vertical)
        for key in ('brightness', 'dimensions', 'filter_tools'):
            self.tabifyDockWidget(self.docks['aspect'], self.docks[key])
        self.docks['aspect'].raise_()
        self.splitDockWidget(self.docks['work'], self.docks['labels'], Qt.Vertical)
        self.resizeDocks([self.docks['color'], self.docks['work']], [275, 290], Qt.Horizontal)
        self.resizeDocks([self.docks['recommendations']], [255], Qt.Vertical)
        self.default_panel_layout = self.saveState(1)

    def add_panel(self, key, title, widget, area, scroll=False):
        dock = QDockWidget(title, self)
        dock.setObjectName('library.panel.' + key)
        dock.toggleViewAction().setText({'brightness': '明度筛选', 'aspect': '图片比例筛选',
                                         'dimensions': '图片尺寸筛选'}.get(key, title))
        dock.setAllowedAreas(Qt.AllDockWidgetAreas)
        dock.setFeatures(QDockWidget.DockWidgetClosable | QDockWidget.DockWidgetMovable |
                         QDockWidget.DockWidgetFloatable)
        dock.setMinimumWidth(230)
        if scroll:
            wrapper = QScrollArea()
            wrapper.setWidgetResizable(True)
            wrapper.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            wrapper.setWidget(widget)
            widget = wrapper
        dock.setWidget(widget)
        self.addDockWidget(area, dock)
        self.docks[key] = dock
        return dock

    def toggle_filter_panels(self):
        panels = [self.docks[key] for key in self.filter_panel.sections]
        show = all(panel.isHidden() for panel in panels)
        for panel in panels:
            panel.setVisible(show)

    def reset_panel_layout(self):
        self.restoreState(self.default_panel_layout, 1)
        self.docks['aspect'].raise_()
        self.statusBar().showMessage('已恢复默认面板布局；筛选条件保持不变')

    def restore_panel_layout(self, encoded):
        if not isinstance(encoded, str) or len(encoded) > 100000:
            return
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            return
        if not self.restoreState(data, 1):
            self.restoreState(self.default_panel_layout, 1)
        for dock in self.docks.values():
            if dock.isFloating():
                self.controller.ensure_onscreen(dock)

    def action(self, menu, title, callback, shortcut=None):
        action = QAction(title, self)
        action.triggered.connect(lambda checked=False: callback())
        if shortcut:
            action.setShortcut(QKeySequence(shortcut))
        menu.addAction(action)
        return action

    def _menus(self):
        file = self.menuBar().addMenu('文件')
        self.action(file, '导入图片…', self.import_images, 'Ctrl+O')
        self.action(file, '新建图片库窗口', self.controller.new_library, 'Ctrl+N')
        self.action(file, '连接 Snipaste…', self.controller.choose_source)
        self.action(file, '备份收藏库…', self.backup)
        self.action(file, '从备份恢复到新图库…', self.restore_backup)
        self.action(file, '关闭此窗口', self.close, 'Ctrl+W')
        self.action(file, '退出应用', self.controller.quit, 'Ctrl+Q')
        edit = self.menuBar().addMenu('编辑')
        self.action(edit, '备注与标签…', self.annotate)
        self.action(edit, '清除筛选', self.clear_filters)
        self.action(edit, '恢复已移除的内容…', self.recovery)
        view = self.menuBar().addMenu('视图')
        self.action(view, '返回缩略图', self.return_to_grid, 'Esc')
        self.action(view, '上一张', lambda: self.step_photo(-1), 'Left')
        self.action(view, '下一张', lambda: self.step_photo(1), 'Right')
        self.action(view, '适合窗口', self.detail_view.fit, 'F')
        self.action(view, '实际大小', self.detail_view.actual, '1')
        self.action(view, '显示／隐藏全部筛选面板', self.toggle_filter_panels, 'Tab')
        groups = self.menuBar().addMenu('标签与参考组')
        self.action(groups, '全部图片', self.select_all)
        self.action(groups, '在查看器中打开当前选择', self.open_board)
        self.action(groups, '新建作品参考组', self.create_work)
        self.action(groups, '刷新贴图图组', self.controller.sync, 'F5')
        windows = self.menuBar().addMenu('窗口')
        self.action(windows, '新建图片库窗口', self.controller.new_library)
        self.action(windows, '左右并排', lambda: self.controller.tile(True))
        self.action(windows, '上下排列', lambda: self.controller.tile(False))
        self.action(windows, '移至下一显示器', self.next_screen)
        self.action(windows, '恢复保存的窗口布局', self.controller.restore_windows)
        windows.addSeparator()
        for dock in self.docks.values():
            windows.addAction(dock.toggleViewAction())
        windows.addSeparator()
        self.action(windows, '恢复默认面板布局', self.reset_panel_layout)
        settings = self.menuBar().addMenu('设置')
        self.action(settings, '设置…', self.controller.settings)
        self.action(settings, '索引状态与问题', self.controller.diagnostics)
        self.action(settings, '检查图库完整性…', lambda: self.check_integrity(False))
        self.action(settings, '重建缩略图与索引…', lambda: self.check_integrity(True))
        help_menu = self.menuBar().addMenu('帮助')
        self.action(help_menu, '快捷操作', lambda: QMessageBox.information(self, '快捷操作',
            '单击缩略图：原位放大；Ctrl＋单击多选\n拖缩略图到参考组面板：加入或新建参考组\n拖缩略图到标签面板：加入已有标签\nEsc：返回网格\nCtrl+N：新建图片库窗口\nCtrl+Alt+B：优先打开当前工作组\n查看器 Page Up / Page Down：切换标签或参考组\n图片右下角按住拖动：旋转\n查看器 Alt＋左拖：移动窗口'))

    def update_groups(self):
        previous = self.restoring
        self.restoring = True
        self.groups.clear()
        labels = self.catalog.label_rows()
        valid = {g['id'] for g in labels}
        if self.selected_groups is not None:
            self.selected_groups = [g for g in self.selected_groups if g in valid] or None
        for group in labels:
            prefix = {'source': 'S', 'local': 'L', 'manual': '#'}[group['kind']]
            item = QListWidgetItem(f"{prefix}  {group['name']}   {group['count']}")
            item.setToolTip({'source': 'Snipaste 自动标签：', 'local': '本地导入图片：', 'manual': '手动标签：'}[group['kind']] + group['name'])
            item.setData(Qt.UserRole, group['id'])
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if group['id'] in (self.selected_groups or []) else Qt.Unchecked)
            self.groups.addItem(item)
        self.work_list.clear()
        work_groups = self.catalog.work_groups()
        if self.selected_work is not None and not any(g['id'] == self.selected_work for g in work_groups):
            self.selected_work = None
        self.current_work_label.setText('尚未设置当前工作组\n选组后点“★ 当前”启用自动收集')
        for group in work_groups:
            item = QListWidgetItem(f"{'★ ' if group['current'] else ''}{group['name']}   {group['count']}")
            item.setData(Qt.UserRole, group['id'])
            if group['current']:
                item.setBackground(QColor('#244940'))
                item.setForeground(QColor('#b2f1d6'))
                font = item.font()
                font.setBold(True)
                item.setFont(font)
                self.current_work_label.setText(f"当前：{group['name']}\n新增贴图自动加入 · 快捷键优先打开")
            self.work_list.addItem(item)
            if group['id'] == self.selected_work:
                self.work_list.setCurrentItem(item)
        self.restoring = previous

    def changed_collections(self):
        self.controller.changed.emit()
        if self.controller.board:
            self.controller.board.update_groups()

    def create_work(self):
        name, ok = QInputDialog.getText(self, '新建作品参考组', '名称', text=f'参考组 {len(self.catalog.work_groups()) + 1}')
        if ok and name.strip():
            self.selected_work = self.catalog.create_work_group(name)
            self.changed_collections()

    def drop_to_work(self, ident, digests):
        if ident is None:
            ident = self.catalog.create_work_group(f'参考组 {len(self.catalog.work_groups()) + 1}', digests)
            self.statusBar().showMessage('已建立参考组，可双击组名重命名')
        else:
            count = self.catalog.add_to_work_group(ident, digests)
            self.statusBar().showMessage(f'已加入参考组：{count} 张新图片')
        self.changed_collections()

    def select_work(self, ident):
        self.selected_work = ident
        self.update_groups()
        self.return_to_grid()
        self.refresh()

    def drop_to_label(self, ident, digests):
        count = self.catalog.add_to_label(ident, digests)
        self.changed_collections()
        self.statusBar().showMessage(f'已加入标签：{count} 张新图片' if count else '图片已在此标签中，无需重复加入')

    def rename_work(self):
        item = self.work_list.currentItem()
        if item is None:
            return
        ident = item.data(Qt.UserRole)
        group = next(g for g in self.catalog.work_groups() if g['id'] == ident)
        name, ok = QInputDialog.getText(self, '重命名参考组', '名称', text=group['name'])
        if ok and name.strip():
            self.catalog.rename_work_group(ident, name)
            self.changed_collections()

    def activate_work(self):
        item = self.work_list.currentItem()
        if item:
            self.catalog.set_current_work_group(item.data(Qt.UserRole))
            self.changed_collections()
            self.controller.sync()

    def label_menu(self, position):
        item = self.groups.itemAt(position)
        if item is None:
            return
        ident = item.data(Qt.UserRole)
        menu = QMenu(self)
        menu.addAction('移除此标签（保留图片）', lambda: self.remove_label(ident))
        menu.exec(self.groups.viewport().mapToGlobal(position))

    def remove_label(self, ident):
        if self.catalog.remove_label(ident):
            self.changed_collections()
            self.statusBar().showMessage('标签已移除，图片保留；自动同步不会恢复此标签')

    def photo_menu(self, grid, position):
        index = grid.indexAt(position)
        if not index.isValid():
            return
        selected = grid.selectionModel().selectedIndexes()
        targets = selected if index in selected else [index]
        digests = [i.data(Qt.UserRole)['digest'] for i in targets]
        menu = QMenu(self)
        title = f'从图片库删除所选 {len(digests)} 张图片（保留源文件）' if len(digests) > 1 else '从图片库删除图片（保留源文件）'
        menu.addAction(title, lambda: self.controller.delete_images(digests))
        menu.exec(grid.viewport().mapToGlobal(position))

    def detail_menu(self, position):
        if self.current is None or self.detail_view.itemAt(position) is None:
            return
        digest = self.current['digest']
        menu = QMenu(self)
        menu.addAction('从图片库删除图片（保留源文件）', lambda: self.controller.delete_images([digest]))
        menu.exec(self.detail_view.viewport().mapToGlobal(position))

    def work_menu(self, position):
        item = self.work_list.itemAt(position)
        if item:
            self.work_list.setCurrentItem(item)
        menu = QMenu(self)
        if item:
            menu.addAction('设为当前工作组', self.activate_work)
            menu.addAction('重命名', self.rename_work)
            ident = item.data(Qt.UserRole)
            menu.addAction('在查看器中打开', lambda: self.controller.show_board(f'work:{ident}'))
            if self.current:
                menu.addAction('将当前大图加入此组', lambda: self.drop_to_work(ident, [self.current['digest']]))
                menu.addAction('将当前大图移出此组', lambda: self.remove_photo(ident))
            menu.addSeparator()
            menu.addAction('删除参考组（保留图片）', lambda: self.delete_work(ident))
        menu.addAction('取消当前工作组／停止自动收集', self.deactivate_work)
        menu.exec(self.work_list.viewport().mapToGlobal(position))

    def delete_work(self, ident):
        if self.catalog.delete_work_group(ident):
            self.changed_collections()
            self.statusBar().showMessage('参考组已删除，图片和标签已保留')

    def deactivate_work(self):
        self.catalog.set_current_work_group(None)
        self.changed_collections()

    def remove_photo(self, ident):
        if self.current:
            self.catalog.remove_from_work_group(ident, [self.current['digest']])
            self.changed_collections()

    def add_current_photo(self):
        ident = self.catalog.get_setting('work.current')
        if not ident:
            self.statusBar().showMessage('请在右上方选择参考组，并点击“★ 当前”')
        elif self.current:
            self.drop_to_work(ident, [self.current['digest']])

    def save_tags(self):
        if self.current:
            tags = self.tag_editor.text().replace('，', ',').split(',')
            self.catalog.set_tags(self.current['digest'], tags)
            self.current['manual_tags'] = self.catalog.tags_for(self.current['digest'])
            self.tag_editor.setText('，'.join(self.current['manual_tags']))
            self.changed_collections()
            self.statusBar().showMessage('手动标签已保存')

    def filters_changed(self):
        if self.restoring:
            return
        color = self.filter_panel.state()['color']
        self.colors = [color] if color else []
        self.return_to_grid()
        self.query_timer.start(180)

    def groups_changed(self, _item):
        if not self.restoring:
            self.selected_groups = [self.groups.item(i).data(Qt.UserRole) for i in range(self.groups.count())
                                    if self.groups.item(i).checkState() == Qt.Checked]
            self.return_to_grid()
            self.refresh()

    def select_all(self):
        self.selected_groups = None
        self.update_groups()
        self.return_to_grid()
        self.refresh()

    def select_group(self, group_id):
        self.selected_groups = [group_id]
        self.update_groups()
        self.return_to_grid()
        self.refresh()

    def toggle_color(self, color, checked):
        if checked and color not in self.colors:
            self.colors.append(color)
        elif not checked and color in self.colors:
            self.colors.remove(color)
        self.update_color_label()
        self.return_to_grid()
        self.refresh()

    def update_color_label(self):
        state = self.filter_panel.state()
        state['color'] = self.colors[-1] if self.colors else None
        self.filter_panel.restore(state)

    def custom_color(self):
        color = QColorDialog.getColor(QColor('#4385db'), self, '选择筛选颜色')
        if color.isValid():
            self.toggle_color(color.name(), True)

    def clear_filters(self):
        self.restoring = True
        self.colors = []
        self.selected_work = None
        self.filter_panel.restore({})
        self.search.clear()
        self.archived.setChecked(False)
        self.restoring = False
        self.select_all()

    def refresh(self):
        if self.restoring:
            return
        self.query_generation += 1
        generation = self.query_generation
        boards = None if self.selected_groups is None else list(self.selected_groups)
        colors, text, tolerance = list(self.colors), self.search.text(), self.tolerance.value()
        archived, sort = self.archived.isChecked(), self.sort.currentIndex()
        properties = self.filter_panel.state()
        properties['color'] = colors[-1] if colors else None
        properties['tolerance'] = tolerance
        work_group = self.selected_work
        match_all = self.label_mode.currentIndex() == 0
        root = self.catalog.root
        def query():
            with Catalog(root) as catalog:
                rows = catalog.query_filtered(boards, work_group, text, archived, properties, match_all)
            if sort == 1: rows.reverse()
            elif sort == 2: rows.sort(key=lambda r: r['source_name'].casefold())
            elif sort == 3: rows.sort(key=lambda r: r['width'], reverse=True)
            return rows
        self.controller.jobs.submit(query, lambda rows: self._queried(generation, rows),
                                    lambda error: self.statusBar().showMessage(str(error)), owner=self, key='query')

    def _queried(self, generation, rows):
        if generation != self.query_generation:
            return
        self.rows = rows
        scroll = self.grid.verticalScrollBar().value()
        self.model.replace(rows)
        QTimer.singleShot(0, lambda: self.grid.verticalScrollBar().setValue(scroll))
        scope = '图片库' if not self.selected_groups else f'已选 {len(self.selected_groups)} 个标签'
        if self.selected_work is not None:
            group = next((g for g in self.catalog.work_groups() if g['id'] == self.selected_work), None)
            scope = (group['name'] if group else '参考组') + ' · ' + scope
        self.heading.setText(scope)
        self.summary.setText(f'{len(rows)} 张图片' + (' · 颜色筛选已开启' if self.colors else '') +
                             (' · 已保留收藏' if self.archived.isChecked() else '') +
                             (' · 没有匹配图片，可调整两侧交叉筛选' if not rows else ''))
        if self.pending_restore is not None:
            state, self.pending_restore = self.pending_restore, None
            position = state.get('scroll', 0)
            self.grid_position = position
            QTimer.singleShot(0, lambda: self.grid.verticalScrollBar().setValue(position))
            restored = next((row for row in rows if row['digest'] == state.get('current')), None)
            if restored:
                self.show_photo(restored)
                self.grid_position = position
            return
        if self.current and not any(row['digest'] == self.current['digest'] for row in rows):
            self.return_to_grid()
        elif self.current:
            updated = next(row for row in rows if row['digest'] == self.current['digest'])
            if self.current.get('manual_tags') != updated.get('manual_tags'):
                self.tag_editor.setText('，'.join(updated['manual_tags']))
            self.current = updated
            self.source_tags.setText('来源标签：' + ' · '.join(name for _, name in self.current['memberships']))
            self.request_recommendations()

    def show_photo(self, row, record=True):
        if not row or not self.catalog.has_image(row['digest']):
            return
        # Navigation history can outlive label edits; refresh the displayed metadata.
        row = dict(row)
        visible_labels = {g['id'] for g in self.catalog.label_rows()}
        row['memberships'] = [m for m in row['memberships'] if m[0] in visible_labels]
        row['board_name'] = row['memberships'][0][1] if row['memberships'] else '未分类'
        row['manual_tags'] = self.catalog.tags_for(row['digest'])
        if self.stack.currentIndex() == 0:
            self.grid_position = self.grid.verticalScrollBar().value()
        self.current = row
        self.tag_editor.setText('，'.join(self.catalog.tags_for(row['digest'])))
        self.source_tags.setText('来源标签：' + ' · '.join(name for _, name in row['memberships']))
        self.stack.setCurrentIndex(1)
        self.detail_view.display(row)
        self.summary.setText(f"{row['source_name']}  ·  {row['width']} × {row['height']}  ·  {row['board_name']}")
        if record:
            self.nav = self.nav[:self.nav_index + 1] + [row]
            self.nav_index = len(self.nav) - 1
        self.request_recommendations()

    def return_to_grid(self):
        self.rec_generation += 1
        self.current = None
        self.detail_view.row = None
        self.detail_view.scene_model.clear()
        self.detail_view.image_item = None
        self.stack.setCurrentIndex(0)
        self.summary.setText(f'{len(self.rows)} 张图片')
        QTimer.singleShot(0, lambda: self.grid.verticalScrollBar().setValue(self.grid_position))

    def back(self):
        if self.stack.currentIndex() == 1 and self.nav_index > 0:
            self.nav_index -= 1
            self.show_photo(self.nav[self.nav_index], record=False)
        else:
            self.return_to_grid()

    def forward(self):
        if self.nav_index + 1 < len(self.nav):
            self.nav_index += 1
            self.show_photo(self.nav[self.nav_index], record=False)

    def step_photo(self, delta):
        if not self.current or not self.rows:
            return
        index = next((i for i, r in enumerate(self.rows) if r['digest'] == self.current['digest']), 0)
        self.show_photo(self.rows[(index + delta) % len(self.rows)])

    def request_recommendations(self):
        if not self.current:
            return
        self.rec_generation += 1
        generation, current, scope = self.rec_generation, self.current, self.rec_scope.currentIndex()
        candidates, groups, root = list(self.rows), self.selected_groups, self.catalog.root
        work_group, match_all = self.selected_work, self.label_mode.currentIndex() == 0
        for model in self.rec_models:
            model.replace([])
        self.rec_status.setText('正在查找相似图片…')
        def work():
            if scope:
                with Catalog(root) as catalog:
                    pool = catalog.query_filtered(groups, work_group, match_all=match_all) if scope == 1 else catalog.query()
            else:
                pool = candidates
            return Catalog.recommend(current, pool, 100)
        self.controller.jobs.submit(work, lambda rows: self._recommendations(generation, rows), owner=self, key='recommend')

    def _recommendations(self, generation, rows):
        if generation != self.rec_generation or self.current is None:
            return
        self.recommended = rows
        self.reflow_recommendations()
        self.rec_status.setText('点击推荐图可在上方直接查看' if rows else '当前范围内没有其他可推荐图片')

    def eventFilter(self, watched, event):
        if (hasattr(self, 'rec_reflow_timer') and watched is self.rec_views[0].viewport()
                and event.type() == QEvent.Resize):
            self.rec_reflow_timer.start(0)
        return super().eventFilter(watched, event)

    def reflow_recommendations(self):
        columns = max(1, self.rec_views[0].viewport().width() // 128)
        self.rec_models[0].replace(self.recommended[:columns])
        self.rec_models[1].replace(self.recommended[columns:columns * 2])

    def resize_thumbnails(self, size):
        if hasattr(self, 'delegate'):
            self.delegate.size = size
            self.grid.doItemsLayout()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, 'rec_views'):
            self.reflow_recommendations()

    def _repaint_images(self, _path=''):
        self.grid.viewport().update()
        for view in self.rec_views:
            view.viewport().update()

    def source_changed(self):
        self.rec_generation += 1
        self.recommended = []
        for model in self.rec_models:
            model.replace([])
        self.nav = [row for row in self.nav if self.catalog.has_image(row['digest'])]
        self.nav_index = min(self.nav_index, len(self.nav) - 1)
        if self.current and not self.catalog.has_image(self.current['digest']):
            self.return_to_grid()
        self.update_groups()
        self.refresh()

    def open_board(self):
        group = f'work:{self.selected_work}' if self.selected_work is not None else (
            self.selected_groups[0] if self.selected_groups and len(self.selected_groups) == 1 else None)
        self.controller.show_board(group)

    def annotate(self):
        if not self.current:
            return
        note, ok = QInputDialog.getMultiLineText(self, '图片备注', '备注', self.current.get('note', ''))
        if not ok:
            return
        import json
        tags = self.catalog.tags_for(self.current['digest'])
        text, ok = QInputDialog.getText(self, '图片标签', '标签（逗号分隔）', text=', '.join(tags))
        if ok:
            self.catalog.annotate(self.current['id'], note, text.replace('，', ',').split(','))
            self.current['note'], self.current['tags'] = note, json.dumps(text.replace('，', ',').split(','))
            self.tag_editor.setText('，'.join(self.catalog.tags_for(self.current['digest'])))
            self.changed_collections()

    def import_images(self):
        controller = self.controller
        if getattr(controller, 'import_running', False):
            self.statusBar().showMessage('正在导入图片，请等待本批完成')
            return
        paths, _ = QFileDialog.getOpenFileNames(self, '导入图片（可多选；动图和多页图片取第一帧／页）', '',
            '图片 (*.png *.jpg *.jpeg *.webp *.bmp *.tif *.tiff *.gif)')
        if not paths:
            return
        controller.import_running = True
        root = self.catalog.root
        controller.status.emit(f'正在导入 {len(paths)} 张图片…')
        def work():
            with Catalog(root) as catalog:
                return catalog.import_images(paths, controller.cancel_sync.is_set,
                    lambda count, name: controller.status.emit(f'正在导入 {count}/{len(paths)}：{name}'))
        def done(report):
            controller.import_running = False
            if controller.quitting:
                return
            controller.images.failed.clear()
            controller.changed.emit()
            if controller.board:
                controller.board.update_groups()
            if self in controller.windows and report['board_id'] is not None:
                self.clear_filters()
                self.select_group(report['board_id'])
                self.return_to_grid()
            controller.status.emit(f"导入完成：新增 {report['added']} 张，重复 {report['duplicates']} 张，失败 {len(report['errors'])} 张")
            if report['errors']:
                parent = controller.windows[-1] if controller.windows else None
                box = QMessageBox(QMessageBox.Warning, '部分图片未能导入',
                    f"{len(report['errors'])} 张图片导入失败，其余图片已保留。", parent=parent)
                box.setDetailedText('\n'.join(f"{e['file']}：{e['error']}" for e in report['errors']))
                box.exec()
        def failed(error):
            controller.import_running = False
            if not controller.quitting:
                controller.changed.emit()
                controller.status.emit(f'导入中断，已完成的图片已保留：{error}')
        controller.jobs.submit(work, done, failed)

    def recovery(self):
        from .qt_maintenance import RecoveryDialog
        RecoveryDialog(self.controller, self).exec()

    def check_integrity(self, rebuild):
        from .qt_maintenance import check_library
        check_library(self.controller, rebuild)

    def restore_backup(self):
        from .qt_maintenance import restore_dialog
        restore_dialog(self.controller, self)

    def backup(self):
        path, _ = QFileDialog.getSaveFileName(self, '备份收藏库', 'SnipBoard-backup.zip', 'ZIP (*.zip)')
        if path:
            root = self.catalog.root
            cancelled = self.controller.cancel_sync.is_set
            self.statusBar().showMessage('正在备份收藏库…')
            def work():
                with Catalog(root) as catalog:
                    catalog.backup(Path(path), cancelled)
            self.controller.jobs.submit(work, lambda _: self.statusBar().showMessage('备份完成'),
                                        lambda error: QMessageBox.warning(self, '备份失败', str(error)), owner=self)

    def next_screen(self):
        screens = self.controller.app.screens()
        screen = screens[(screens.index(self.screen()) + 1) % len(screens)]
        self.move(screen.availableGeometry().topLeft())

    def state(self):
        rect = self.normalGeometry() if self.isMaximized() else self.geometry()
        return {'geometry': [rect.x(), rect.y(), rect.width(), rect.height()],
                'panel_layout': base64.b64encode(bytes(self.saveState(1))).decode('ascii'),
                'groups': self.selected_groups, 'colors': self.colors, 'search': self.search.text(),
                'work_group': self.selected_work, 'properties': self.filter_panel.state(),
                'label_mode': self.label_mode.currentIndex(),
                'sort': self.sort.currentIndex(), 'size': self.thumb_size.value(),
                'tolerance': self.tolerance.value(), 'archived': self.archived.isChecked(),
                'scroll': self.grid.verticalScrollBar().value(),
                'current': self.current['digest'] if self.current else None}

    def restore_state(self, state):
        if not isinstance(state, dict):
            return
        self.restoring = True
        self.pending_restore = state
        geometry = state.get('geometry')
        if (isinstance(geometry, list) and len(geometry) == 4 and
                all(isinstance(n, int) and abs(n) < 100000 for n in geometry) and min(geometry[2:]) > 0):
            self.setGeometry(*geometry)
        self.restore_panel_layout(state.get('panel_layout'))
        self.selected_groups = state.get('groups')
        self.selected_work = state.get('work_group')
        self.label_mode.setCurrentIndex(state.get('label_mode', 0))
        self.colors = state.get('colors', [])
        self.filter_panel.restore(state.get('properties', dict(color=self.colors[-1] if self.colors else None)))
        color = self.filter_panel.state()['color']
        self.colors = [color] if color else []
        self.search.setText(state.get('search', ''))
        self.sort.setCurrentIndex(state.get('sort', 0))
        self.thumb_size.setValue(state.get('size', 205))
        self.tolerance.setValue(state.get('tolerance', 38))
        self.archived.setChecked(state.get('archived', False))
        self.grid_position = state.get('scroll', 0)
        self.update_groups()
        self.restoring = False

    def closeEvent(self, event):
        self.query_generation += 1
        self.rec_generation += 1
        self.controller.window_closed(self)
        super().closeEvent(event)
