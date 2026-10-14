"""Explicit, session-only configuration for optional multimodal search."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QFormLayout,
                              QLabel, QLineEdit, QScrollArea, QSpinBox,
                              QVBoxLayout, QWidget)

from .model_search import ModelConfig, ModelSearchError


class ModelSettingsDialog(QDialog):
    def __init__(self, parent=None, existing=None):
        super().__init__(parent)
        self.setWindowTitle('可选模型找图 · 服务设置')
        self.resize(580, 590)
        self.setMinimumSize(420, 400)
        self.config = None
        layout = QVBoxLayout(self)
        heading = QLabel('轻量找图默认可用；模型识图由你选择启用。')
        heading.setWordWrap(True)
        heading.setStyleSheet('font-weight:600; font-size:15px;')
        layout.addWidget(heading)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setStyleSheet('QScrollArea { border:0; }')
        content = QWidget()
        content.setObjectName('dockSurface')
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(4, 4, 4, 4)
        description = QLabel(
            '填写支持图片输入、兼容 Chat Completions 的服务。远程服务会收到本次找图范围中的'
            '候选图片缩略图及找图描述；费用与数据处理方式由你填写的服务决定。\n\n'
            '本机服务也会收到这些内容，并占用本机资源；是否影响绘画取决于该服务运行的模型。'
            'SnipBoard 不会替你安装或启动模型。')
        description.setWordWrap(True)
        description.setStyleSheet('color:#b2c1cd;')
        content_layout.addWidget(description)

        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(QFormLayout.WrapLongRows)
        self.base_url_input = QLineEdit()
        self.base_url_input.setObjectName('modelBaseUrl')
        self.base_url_input.setPlaceholderText('https://你的服务地址/v1')
        self.model_input = QLineEdit()
        self.model_input.setObjectName('modelName')
        self.model_input.setPlaceholderText('服务提供的视觉模型名称')
        self.api_key_input = QLineEdit()
        self.api_key_input.setObjectName('modelApiKey')
        self.api_key_input.setEchoMode(QLineEdit.Password)
        self.api_key_input.setPlaceholderText('仅本次运行保留；本机服务可留空')
        form.addRow('服务地址', self.base_url_input)
        form.addRow('模型名称', self.model_input)
        form.addRow('API 密钥', self.api_key_input)
        self.candidate_limit = QSpinBox()
        self.candidate_limit.setRange(1, 40)
        self.candidate_limit.setValue(12)
        self.candidate_limit.setSuffix(' 张')
        self.candidate_limit.setToolTip('每次最多发送这些候选缩略图，避免全库分析。')
        self.batch_size = QSpinBox()
        self.batch_size.setRange(1, 4)
        self.batch_size.setValue(4)
        self.batch_size.setSuffix(' 张')
        self.timeout = QSpinBox()
        self.timeout.setRange(10, 60)
        self.timeout.setValue(30)
        self.timeout.setSuffix(' 秒')
        form.addRow('每次分析上限', self.candidate_limit)
        form.addRow('每批图片数量', self.batch_size)
        form.addRow('请求超时', self.timeout)
        content_layout.addLayout(form)

        hint = QLabel(
            '本机地址示例：http://127.0.0.1:1234/v1。远程地址须使用 HTTPS。\n'
            '设置和密钥仅保留在本次运行的内存中，退出软件后需重新填写。')
        hint.setWordWrap(True)
        hint.setStyleSheet('color:#91a8b8; font-size:12px;')
        content_layout.addWidget(hint)
        self.consent = QCheckBox(
            '允许在模型模式点击找图时，将缩略图发送到这个服务')
        self.consent.setObjectName('modelTransmissionConsent')
        self.consent.setChecked(False)
        content_layout.addWidget(self.consent)
        action_note = QLabel('只有明确点击模型找图才发送；更改设置或打开图库不会自动发送图片。')
        action_note.setWordWrap(True)
        action_note.setStyleSheet('color:#91a8b8; font-size:12px;')
        content_layout.addWidget(action_note)
        self.error_label = QLabel()
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet('color:#d9b887;')
        content_layout.addWidget(self.error_label)
        content_layout.addStretch()
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)

        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.apply_button = self.buttons.button(QDialogButtonBox.Ok)
        self.apply_button.setText('启用此服务')
        self.buttons.button(QDialogButtonBox.Cancel).setText('取消')
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

        if existing is not None:
            self.base_url_input.setText(existing.base_url)
            self.model_input.setText(existing.model)
            self.api_key_input.setText(existing.api_key)
            self.candidate_limit.setValue(existing.candidate_limit)
            self.batch_size.setValue(existing.batch_size)
            self.timeout.setValue(int(existing.timeout))
        for field in (self.base_url_input, self.model_input, self.api_key_input):
            field.textChanged.connect(self._update_apply_state)
        self.consent.toggled.connect(self._update_apply_state)
        self._update_apply_state()

    def _configuration(self):
        config = ModelConfig(
            base_url=self.base_url_input.text().strip(),
            model=self.model_input.text().strip(),
            api_key=self.api_key_input.text().strip(),
            candidate_limit=self.candidate_limit.value(),
            batch_size=self.batch_size.value(),
            timeout=self.timeout.value())
        config.validate()
        return config

    def _update_apply_state(self, *_args):
        valid = False
        error = ''
        if self.consent.isChecked():
            try:
                self._configuration()
                valid = True
            except ModelSearchError as failure:
                error = str(failure)
        self.apply_button.setEnabled(valid)
        self.error_label.setText(error)

    def accept(self):
        if not self.consent.isChecked():
            self.error_label.setText('请先确认允许发送候选缩略图，再启用模型服务。')
            return
        try:
            self.config = self._configuration()
        except ModelSearchError as failure:
            self.error_label.setText(str(failure))
            return
        super().accept()

    def done(self, result):
        if result != QDialog.Accepted:
            self.config = None
        self.api_key_input.clear()
        super().done(result)


def configure_model(parent, existing=None):
    """Return an explicitly allowed config in memory, or None on dismissal."""
    dialog = ModelSettingsDialog(parent, existing)
    try:
        return dialog.config if dialog.exec() == QDialog.Accepted else None
    finally:
        dialog.deleteLater()
