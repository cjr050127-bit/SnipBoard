"""On-demand local natural-language search and optional visual model service."""
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
                              QPlainTextEdit, QComboBox, QCheckBox, QScrollArea)


class FinderPanel(QWidget):
    submitted = Signal(str, bool)
    conditionRemoved = Signal(str)
    clearRequested = Signal()
    cancelRequested = Signal()
    modelSettingsRequested = Signal()
    modeChanged = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('dockSurface')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)
        header = QHBoxLayout()
        title = QLabel('描述一下，你想找什么？')
        title.setStyleSheet('font-size:15px; font-weight:600;')
        header.addWidget(title, 1)
        badge = QLabel('按需找图')
        badge.setStyleSheet('color:#b0e2d7; background:#24443f; padding:4px 7px; border-radius:5px; font-size:11px;')
        header.addWidget(badge)
        layout.addLayout(header)
        note = QLabel('默认按已有信息轻量找图。需要理解画面内容时，可选择接入视觉大模型。')
        note.setWordWrap(True)
        note.setStyleSheet('color:#a5b1bd; font-size:12px;')
        layout.addWidget(note)
        modes = QHBoxLayout()
        self.mode = QComboBox()
        self.mode.addItems(['轻量本地筛选', '大模型视觉筛选'])
        self.mode.currentIndexChanged.connect(self._mode_changed)
        modes.addWidget(self.mode, 1)
        settings = QPushButton('接入模型…')
        settings.clicked.connect(self.modelSettingsRequested)
        modes.addWidget(settings)
        layout.addLayout(modes)
        self.model_hint = QLabel('轻量模式无需配置；未启用画面内容识别。')
        self.model_hint.setWordWrap(True)
        self.model_hint.setTextFormat(Qt.PlainText)
        self.model_hint.setStyleSheet('color:#a5b1bd; font-size:12px;')
        layout.addWidget(self.model_hint)
        self.query = QPlainTextEdit()
        self.query.setObjectName('finderQuery')
        self.query.setPlaceholderText('例如：人物标签里的横图，宽度至少 1600，不要教程')
        self.query.setFixedHeight(100)
        self.query.setStyleSheet('QPlainTextEdit { background:#202328; color:#e3e5e9; border:1px solid #46575e; border-radius:7px; padding:7px; } QPlainTextEdit:focus { border-color:#91c7ca; }')
        layout.addWidget(self.query)
        self.scope = QComboBox()
        self.scope.addItems(['整个图库', '当前筛选结果', '当前工作组'])
        self.scope.setToolTip('当前筛选结果按点击找图时的范围继续筛选')
        layout.addWidget(self.scope)
        self.continue_query = QCheckBox('接着上次找图，继续缩小范围')
        self.continue_query.setEnabled(False)
        layout.addWidget(self.continue_query)
        buttons = QHBoxLayout()
        self.search_button = QPushButton('找图  Ctrl+Enter')
        self.search_button.setObjectName('finderSubmit')
        self.search_button.setStyleSheet('QPushButton { background:#88bdbd; color:#132729; border:0; font-weight:600; padding:9px; } QPushButton:hover { background:#a2d4cf; }')
        self.search_button.clicked.connect(self.submit)
        buttons.addWidget(self.search_button, 1)
        self.cancel_button = QPushButton('停止')
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancelRequested)
        buttons.addWidget(self.cancel_button)
        layout.addLayout(buttons)
        shortcut = QShortcut(QKeySequence('Ctrl+Return'), self)
        shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        shortcut.activated.connect(self.submit)
        self.shortcut = shortcut
        examples = QComboBox()
        examples.addItems(['试一句…', '找横图，宽度至少 1600', '蓝色为主的竖图', '人物，不要教程'])
        examples.activated.connect(lambda index: self.query.setPlainText(examples.itemText(index)) if index else None)
        layout.addWidget(examples)
        condition_heading = QLabel('识别出的条件 · 点击 × 移除')
        condition_heading.setStyleSheet('color:#a5b1bd; font-size:12px;')
        layout.addWidget(condition_heading)
        self.conditions = QWidget()
        self.conditions.setObjectName('finderConditions')
        self.conditions.setStyleSheet('QWidget#finderConditions { background:#17191d; }')
        self.condition_layout = QVBoxLayout(self.conditions)
        self.condition_layout.setContentsMargins(0, 0, 0, 0)
        self.condition_layout.setSpacing(6)
        self.condition_scroll = QScrollArea()
        self.condition_scroll.setWidgetResizable(True)
        self.condition_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.condition_scroll.setWidget(self.conditions)
        self.condition_scroll.setMinimumHeight(65)
        self.condition_scroll.setMaximumHeight(210)
        self.condition_scroll.setStyleSheet('QScrollArea { border:none; background:transparent; }')
        layout.addWidget(self.condition_scroll, 1)
        self.warning = QLabel()
        self.warning.setWordWrap(True)
        self.warning.setStyleSheet('color:#d9b887; font-size:12px;')
        self.warning.hide()
        layout.addWidget(self.warning)
        self.status = QLabel('点击找图才运行；图片留在本机。')
        self.status.setWordWrap(True)
        self.status.setStyleSheet('color:#a6c7c9; font-size:12px;')
        layout.addWidget(self.status)
        self.clear_button = QPushButton('返回原筛选')
        self.clear_button.clicked.connect(self.clearRequested)
        layout.addWidget(self.clear_button)
        self.condition_buttons = {}
        self.model_description = '请先接入支持图片输入的模型。'
        self.set_plan(None)

    def _mode_changed(self, *_):
        visual = self.mode.currentIndex() == 1
        self.search_button.setText('用模型找图  Ctrl+Enter' if visual else '找图  Ctrl+Enter')
        self.model_hint.setText(self.model_description if visual else '轻量模式无需配置；未启用画面内容识别。')
        self.modeChanged.emit()

    def set_model(self, config):
        if config is not None:
            from urllib.parse import urlsplit
            host = urlsplit(config.base_url).hostname or '模型服务'
            self.model_description = f'{config.model} · {host}\n点击找图时发送缩略图，最多分析最近 {config.candidate_limit} 张候选；不自动扫描图库。'
        else:
            self.model_description = '请先接入支持图片输入的模型。'
        self._mode_changed()

    def submit(self):
        text = self.query.toPlainText().strip()
        if len(text) > 500:
            self.set_status('请将描述缩短到 500 个字以内。')
            return
        if not text:
            self.set_status('先输入想找的图片或筛选条件。')
            self.query.setFocus()
            return
        self.submitted.emit(text, self.continue_query.isChecked())

    def set_plan(self, plan):
        visual = self.mode.currentIndex() == 1
        while self.condition_layout.count():
            item = self.condition_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.condition_buttons = {}
        self.continue_query.setEnabled(plan is not None and bool(plan.conditions))
        if not plan:
            self.continue_query.setChecked(False)
            label = QLabel('你的条件会显示在这里')
            label.setStyleSheet('color:#788791; padding:8px 0;')
            self.condition_layout.addWidget(label)
            self.show_warnings(())
        else:
            for condition in plan.conditions:
                card = QWidget()
                card.setStyleSheet('background:#253139; border-radius:5px;')
                row = QHBoxLayout(card)
                row.setContentsMargins(8, 3, 3, 3)
                if visual:
                    from .model_search import model_condition_text
                    caption = model_condition_text(plan, condition)
                else:
                    caption = condition.text
                text = QLabel(caption)
                text.setTextFormat(Qt.PlainText)
                text.setWordWrap(True)
                text.setStyleSheet('color:#cee1e5; font-size:12px;')
                row.addWidget(text, 1)
                remove = QPushButton('×')
                remove.setFixedSize(26, 26)
                remove.setToolTip('移除这个条件，重新找图')
                remove.setStyleSheet('QPushButton { border:0; background:transparent; padding:0; color:#abc7ce; font-size:18px; } QPushButton:hover { background:#455b66; }')
                remove.clicked.connect(lambda _checked=False, ident=condition.id: self.conditionRemoved.emit(ident))
                row.addWidget(remove)
                self.condition_buttons[condition.id] = remove
                self.condition_layout.addWidget(card)
            from .query_plan import METADATA_WARNING
            self.show_warnings(tuple(w for w in plan.warnings if not visual or w != METADATA_WARNING))
        self.condition_layout.addStretch()

    def show_warnings(self, warnings):
        self.warning.setText('\n'.join(dict.fromkeys(warnings)))
        self.warning.setVisible(bool(warnings))

    def set_status(self, text, busy=False):
        self.status.setText(text)
        self.cancel_button.setEnabled(busy)
