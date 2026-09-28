"""User-facing recovery and diagnostic workflows for the local library."""
from datetime import datetime
from pathlib import Path
import sys
import uuid

from PySide6.QtCore import Qt, QProcess
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QLabel, QTabWidget, QListWidget,
    QListWidgetItem, QPushButton, QFileDialog, QMessageBox)

from .catalog import Catalog
from .maintenance import inspect_library, restore_backup


class RecoveryDialog(QDialog):
    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.setWindowTitle('恢复已移除的内容')
        self.resize(620, 480)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel('选择需要恢复的项目，可按 Ctrl 或 Shift 多选。'))
        self.tabs = QTabWidget()
        self.lists = []
        for title in ('图片', '标签', '参考组'):
            widget = QListWidget()
            widget.setSelectionMode(QListWidget.ExtendedSelection)
            self.tabs.addTab(widget, title)
            self.lists.append(widget)
        layout.addWidget(self.tabs)
        self.message = QLabel('恢复参考组不会自动启用“当前工作组”。')
        layout.addWidget(self.message)
        button = QPushButton('恢复所选')
        button.clicked.connect(self.restore_selected)
        layout.addWidget(button)
        self.reload()

    def reload(self):
        catalog = self.controller.catalog
        data = [catalog.deleted_rows(), catalog.removed_label_rows(), catalog.deleted_work_rows()]
        for widget, rows in zip(self.lists, data):
            widget.clear()
            for row in rows:
                name = row['name']
                if row.get('kind') == 'manual' and catalog.get_setting('removed-members:' + row['id']) is None:
                    name += '（仅可恢复标签名称）'
                item = QListWidgetItem(name)
                item.setData(Qt.UserRole, row.get('digest', row.get('id')))
                widget.addItem(item)

    def restore_selected(self):
        index = self.tabs.currentIndex()
        identities = [item.data(Qt.UserRole) for item in self.lists[index].selectedItems()]
        if not identities:
            self.message.setText('请先选择需要恢复的项目。')
            return
        catalog = self.controller.catalog
        count = (catalog.restore_images, catalog.restore_labels, catalog.restore_work_groups)[index](identities)
        self.controller.changed.emit()
        if self.controller.board:
            self.controller.board.update_groups()
        self.reload()
        self.message.setText(f'已恢复 {count} 项。恢复的参考组未自动设为当前工作组。')


def check_library(controller, rebuild=False):
    if getattr(controller, 'maintenance_running', False):
        controller.status.emit('图库检查进行中，请等待完成')
        return
    controller.maintenance_running = True
    root = controller.catalog.root
    controller.status.emit('正在检查图库…')
    def work():
        with Catalog(root) as catalog:
            return inspect_library(catalog, rebuild, controller.cancel_sync.is_set,
                lambda index, total: controller.status.emit(f'正在检查图库：{index}/{total}'))
    def done(report):
        controller.maintenance_running = False
        if controller.quitting:
            return
        if rebuild:
            controller.images.cache.clear()
            controller.images.bytes = 0
            controller.images.failed.clear()
            controller.changed.emit()
        text = (f"检查 {report['checked']} 张收藏副本\n数据库问题：{len(report['database'])}\n"
                f"缺失图片：{len(report['missing'])}\n损坏或无法读取：{len(report['corrupt'])}\n"
                f"检查时缺失／损坏缩略图：{len(report['thumbnails'])}\n已重建索引：{report['rebuilt']}")
        box = QMessageBox(QMessageBox.Information, '图库完整性检查', text,
                          parent=controller.windows[-1] if controller.windows else None)
        details = report['database'] + report['missing'] + report['corrupt'] + report['thumbnails']
        if details:
            box.setDetailedText('\n'.join(details))
        box.exec()
        controller.status.emit('图库完整性检查完成')
    def failed(error):
        controller.maintenance_running = False
        if not controller.quitting:
            controller.status.emit(f'图库检查失败：{error}')
    controller.jobs.submit(work, done, failed)


def restore_dialog(controller, parent):
    archive, _ = QFileDialog.getOpenFileName(parent, '选择图库备份', '', 'SnipBoard 备份 (*.zip)')
    if not archive:
        return
    folder = QFileDialog.getExistingDirectory(parent, '选择恢复位置（在其中创建新图库，不覆盖现有图库）')
    if not folder:
        return
    destination = Path(folder) / f'SnipBoard-Restored-{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:4]}'
    controller.status.emit('正在校验备份并恢复到新图库…')
    def done(report):
        if controller.quitting:
            return
        controller.status.emit(f"备份已恢复：{report['path']}")
        answer = QMessageBox.question(controller.windows[-1] if controller.windows else None, '恢复完成',
            f"已恢复 {report['images']} 张图片到：\n{report['path']}\n\n立即打开恢复后的图库？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer == QMessageBox.Yes:
            args = [] if getattr(sys, 'frozen', False) else ['-m', 'snipboard.launcher']
            started, _ = QProcess.startDetached(sys.executable, args + ['--library', report['path']])
            if not started:
                QMessageBox.warning(None, '无法打开', f"图库已恢复，但程序未能启动：\n{report['path']}")
    def failed(error):
        if not controller.quitting:
            QMessageBox.warning(controller.windows[-1] if controller.windows else None, '恢复失败', str(error))
    controller.jobs.submit(lambda: restore_backup(archive, destination, Catalog.schema_version, controller.cancel_sync.is_set), done, failed)
