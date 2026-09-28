"""Nonblocking first-run connection and source relocation UI."""
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel, QListWidget,
                              QListWidgetItem, QPushButton, QFileDialog)

from .discovery import discover_candidates
from .source import normalize_source, history_directory


class ConnectionDialog(QDialog):
    def __init__(self, controller, first_run=False):
        super().__init__(controller.windows[-1] if controller.windows else None)
        self.controller = controller
        self.auto_connect = first_run and controller.source is None
        self.closed = False
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setWindowTitle('连接 Snipaste · SnipBoard')
        self.resize(640, 470)
        layout = QVBoxLayout(self)
        title = QLabel('连接你的 Snipaste 图片')
        title.setObjectName('title')
        layout.addWidget(title)
        description = QLabel('只读取已保存的贴图图组，不导入截图历史。\n连接后自动同步新增贴图，也可以暂时跳过、直接导入本地图片。')
        description.setWordWrap(True)
        layout.addWidget(description)
        self.message = QLabel('正在查找 Snipaste…')
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        if controller.source:
            current = QLabel('已记住的位置：' + str(controller.source) + '\n切换前会保留原来的连接与本地图片。')
            current.setWordWrap(True)
            layout.addWidget(current)
        self.locations = QListWidget()
        self.locations.setWordWrap(True)
        layout.addWidget(self.locations, 1)
        self.locations.itemDoubleClicked.connect(lambda _: self.connect_selected())
        help_text = QLabel('未找到？先启动 Snipaste，再点“重新查找”。\n也可选择 Snipaste.exe、config.ini 或数据文件夹。配置位置可在 Snipaste 的“首选项 → 常规 → 配置文件存储位置”查看。')
        help_text.setWordWrap(True)
        layout.addWidget(help_text)
        row = QHBoxLayout()
        for text, callback in [('重新查找', self.scan), ('选择程序／配置文件…', self.browse_file), ('选择文件夹…', self.browse_folder)]:
            button = QPushButton(text)
            button.clicked.connect(callback)
            row.addWidget(button)
        layout.addLayout(row)
        row = QHBoxLayout()
        self.connect_button = QPushButton('连接所选位置')
        self.connect_button.setEnabled(False)
        self.connect_button.clicked.connect(self.connect_selected)
        self.locations.currentItemChanged.connect(lambda item, _: self.connect_button.setEnabled(item is not None))
        row.addWidget(self.connect_button)
        skip = QPushButton('暂时跳过' if controller.source is None else '保留现有连接')
        skip.clicked.connect(self.reject)
        row.addWidget(skip)
        layout.addLayout(row)
        self.finished.connect(self.completed)
        QTimer.singleShot(0, self.scan)

    def completed(self, _result):
        self.closed = True
        self.controller.catalog.set_setting('connection.setup_done', True)

    def scan(self):
        if self.closed or self.controller.quitting:
            return
        self.message.setText('正在查找运行中的程序、快捷方式及常见位置…')
        self.locations.clear()
        self.controller.jobs.submit(discover_candidates, self.found, self.failed, owner=self, key='discover')

    def found(self, candidates):
        if self.closed or self.controller.quitting:
            return
        self.locations.clear()
        for candidate in candidates:
            item = QListWidgetItem(f"{candidate['reason']} · {candidate['groups']} 个贴图图组\n{candidate['history']}")
            item.setData(Qt.UserRole, candidate['path'])
            item.setToolTip(candidate['path'])
            self.locations.addItem(item)
        if len(candidates) == 1:
            self.locations.setCurrentRow(0)
            self.message.setText('找到一个可用位置，连接后即可查看图片。')
            if self.auto_connect:
                self.connect_selected()
        elif candidates:
            self.message.setText('找到多个位置，请选择你正在使用的 Snipaste 数据。')
        else:
            self.message.setText('尚未找到可用的贴图数据。请启动 Snipaste 并保存至少一张贴图，然后重新查找；也可以手动选择位置。')

    def failed(self, error):
        if not self.closed:
            self.message.setText('自动查找未完成，可重试或手动选择位置。\n' + str(error))

    def browse_file(self):
        self.auto_connect = False
        filename, _ = QFileDialog.getOpenFileName(self, '选择 Snipaste 程序或配置文件', '', 'Snipaste (Snipaste.exe config.ini)')
        if filename:
            self.connect_path(filename)

    def browse_folder(self):
        self.auto_connect = False
        folder = QFileDialog.getExistingDirectory(self, '选择 Snipaste 所在文件夹或贴图数据文件夹')
        if folder:
            self.connect_path(folder)

    def connect_selected(self):
        item = self.locations.currentItem()
        if item:
            self.connect_path(item.data(Qt.UserRole))

    def connect_path(self, path):
        if self.closed or self.controller.quitting:
            return
        if self.controller.sync_running:
            self.message.setText('当前正在同步图片，请稍后再连接。')
            return
        try:
            source = normalize_source(Path(path))
            history = history_directory(source)
            root = self.controller.catalog.root
            if root.is_relative_to(source) or root.is_relative_to(history):
                raise ValueError('请选择 Snipaste 的位置，本应用图库需要保存在来源目录之外。')
        except (OSError, ValueError) as error:
            self.message.setText('无法连接此位置：' + str(error))
            return
        # Reuse an existing identity for the same history, keeping labels and layouts.
        saved = self.controller.source
        if saved:
            try:
                if history_directory(saved) == history:
                    source = saved
            except (OSError, ValueError):
                pass
        self.controller.source = source
        self.controller.catalog.set_setting('source', str(source))
        self.controller.fingerprint = None
        self.controller.status.emit('已连接 Snipaste，正在读取贴图图组…')
        self.controller.sync()
        self.accept()
