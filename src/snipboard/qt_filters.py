"""Continuous HSV picker and intersecting image property filters."""
import math
from PySide6.QtCore import Qt, Signal, QPointF, QRectF
from PySide6.QtGui import QColor, QPainter, QPen, QBrush, QConicalGradient, QLinearGradient
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QCheckBox, QSlider,
                             QSpinBox, QDoubleSpinBox, QComboBox, QLineEdit, QPushButton, QFormLayout)


class ColorWheel(QWidget):
    changed = Signal(str)

    def __init__(self):
        super().__init__()
        self.hue, self.saturation, self.value = .60, .70, .85
        self.mode = None
        self.setFixedSize(214, 214)
        self.setToolTip('外圈选择色相；内方盘横向为饱和度、纵向为明度。也可输入 HEX。')

    def color(self):
        return QColor.fromHsvF(self.hue, self.saturation, self.value)

    def set_color(self, text):
        color = QColor(text)
        if color.isValid():
            hue, saturation, value, _ = color.getHsvF()
            if hue >= 0:
                self.hue = hue
            self.saturation, self.value = saturation, value
            self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        center = QPointF(107, 107)
        gradient = QConicalGradient(center, 0)
        for i in range(13):
            gradient.setColorAt(i / 12, QColor.fromHsvF((i / 12) % 1, 1, 1))
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QBrush(gradient), 18))
        p.drawEllipse(center, 94, 94)
        rect = QRectF(47, 47, 120, 120)
        saturation = QLinearGradient(rect.topLeft(), rect.topRight())
        saturation.setColorAt(0, Qt.white)
        saturation.setColorAt(1, QColor.fromHsvF(self.hue, 1, 1))
        p.fillRect(rect, saturation)
        value = QLinearGradient(rect.topLeft(), rect.bottomLeft())
        value.setColorAt(0, QColor(0, 0, 0, 0))
        value.setColorAt(1, Qt.black)
        p.fillRect(rect, value)
        angle = self.hue * 2 * math.pi
        for point in (QPointF(107 + 94 * math.cos(angle), 107 - 94 * math.sin(angle)),
                      QPointF(47 + 120 * self.saturation, 47 + 120 * (1 - self.value))):
            p.setPen(QPen(Qt.black, 3))
            p.drawEllipse(point, 5, 5)
            p.setPen(QPen(Qt.white, 1.5))
            p.drawEllipse(point, 5, 5)

    def choose(self, point):
        if self.mode == 'hue':
            self.hue = (math.atan2(107 - point.y(), point.x() - 107) / (2 * math.pi)) % 1
        elif self.mode == 'sv':
            self.saturation = max(0, min(1, (point.x() - 47) / 120))
            self.value = max(0, min(1, 1 - (point.y() - 47) / 120))
        self.update()
        self.changed.emit(self.color().name())

    def mousePressEvent(self, event):
        if event.button() != Qt.LeftButton:
            return
        pos = event.position()
        radius = math.hypot(pos.x() - 107, pos.y() - 107)
        self.mode = 'hue' if 82 <= radius <= 106 else 'sv' if QRectF(47, 47, 120, 120).contains(pos) else None
        if self.mode:
            self.choose(pos)

    def mouseMoveEvent(self, event):
        if self.mode:
            self.choose(event.position())

    def mouseReleaseEvent(self, event):
        if self.mode:
            self.choose(event.position())
        self.mode = None


class FilterPanel(QWidget):
    changed = Signal()
    clearRequested = Signal()

    def __init__(self):
        super().__init__()
        self.restoring = False
        self.setObjectName('filterSurface')
        self.sections = {}
        self.section_layout = QVBoxLayout(self)
        layout = self.section('color')
        self.color_enabled = QCheckBox('启用色彩筛选 · HSV 色盘')
        layout.addWidget(self.color_enabled)
        self.wheel = ColorWheel()
        layout.addWidget(self.wheel, 0, Qt.AlignHCenter)
        self.hex = QLineEdit(self.wheel.color().name())
        self.hex.setMaxLength(7)
        self.hex.setPlaceholderText('#RRGGBB')
        self.hex.editingFinished.connect(self.hex_changed)
        layout.addWidget(self.hex)
        self.color_value = QLabel()
        layout.addWidget(self.color_value)
        self.wheel.changed.connect(self.color_changed)
        form = QFormLayout()
        self.tolerance = QSlider(Qt.Horizontal)
        self.tolerance.setRange(1, 80)
        self.tolerance.setValue(30)
        self.tolerance.setToolTip('Lab 色差半径 ΔE76；越大接受的近似颜色越多')
        self.coverage = QSpinBox()
        self.coverage.setRange(1, 100)
        self.coverage.setValue(10)
        self.coverage.setSuffix('%')
        form.addRow('色差容差', self.tolerance)
        form.addRow('色彩最低占比', self.coverage)
        layout.addLayout(form)
        layout = self.section('brightness')
        form = QFormLayout()
        self.brightness_min, self.brightness_max = QSpinBox(), QSpinBox()
        for spin in (self.brightness_min, self.brightness_max):
            spin.setRange(0, 100)
        self.brightness_max.setValue(100)
        form.addRow('平均明度 L* ≥', self.brightness_min)
        form.addRow('平均明度 L* ≤', self.brightness_max)
        layout.addLayout(form)
        layout = self.section('aspect')
        form = QFormLayout()
        self.orientation = QComboBox()
        for label, value in [('不限方向', 'any'), ('横图', 'landscape'), ('竖图', 'portrait'), ('近方图', 'square')]:
            self.orientation.addItem(label, value)
        form.addRow('方向', self.orientation)
        self.ratio = QComboBox()
        for label, value in [('不限比例', 0), ('1:1', 1), ('4:3', 4/3), ('3:4', 3/4), ('16:9', 16/9), ('9:16', 9/16), ('3:2', 1.5), ('2:3', 2/3), ('自定义', -1)]:
            self.ratio.addItem(label, value)
        form.addRow('宽高比 ±5%', self.ratio)
        self.ratio_value = QDoubleSpinBox()
        self.ratio_value.setRange(.01, 100)
        self.ratio_value.setDecimals(3)
        self.ratio_value.setValue(1)
        self.ratio_value.setEnabled(False)
        self.ratio.currentIndexChanged.connect(lambda _: self.ratio_value.setEnabled(self.ratio.currentData() == -1))
        form.addRow('自定义 宽÷高', self.ratio_value)
        layout.addLayout(form)
        layout = self.section('dimensions')
        form = QFormLayout()
        self.dimensions = {}
        for key, label in [('min_width', '最小宽度'), ('max_width', '最大宽度'), ('min_height', '最小高度'), ('max_height', '最大高度')]:
            spin = QSpinBox()
            spin.setRange(0, 1000000)
            spin.setSpecialValueText('不限')
            self.dimensions[key] = spin
            form.addRow(label, spin)
        layout.addLayout(form)
        layout = self.section('filter_tools')
        tip = QLabel('所有面板中的条件共同筛选；隐藏面板不会清除条件。')
        tip.setWordWrap(True)
        layout.addWidget(tip)
        self.archived = QCheckBox('仅查看源图已移除的收藏')
        layout.addWidget(self.archived)
        clear = QPushButton('清除全部筛选')
        clear.clicked.connect(self.clearRequested)
        layout.addWidget(clear)
        layout.addStretch()
        for control in [self.tolerance, self.coverage, self.brightness_min, self.brightness_max, self.ratio_value, *self.dimensions.values()]:
            control.valueChanged.connect(self.emit_changed)
        for control in (self.orientation, self.ratio):
            control.currentIndexChanged.connect(self.emit_changed)
        self.color_enabled.toggled.connect(self.emit_changed)
        self.archived.toggled.connect(self.emit_changed)
        self.update_readout()

    def section(self, key):
        widget = QWidget(self)
        widget.setObjectName("dockSurface")
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setAlignment(Qt.AlignTop)
        self.sections[key] = widget
        self.section_layout.addWidget(widget)
        return layout

    def emit_changed(self, *_):
        self.update_readout()
        if not self.restoring:
            self.changed.emit()

    def update_readout(self):
        color = self.wheel.color()
        self.color_value.setText(f'H {round(self.wheel.hue*360)}° · S {round(color.saturationF()*100)}% · V {round(color.valueF()*100)}%\n色差半径 ΔE {self.tolerance.value()}')

    def color_changed(self, text):
        self.hex.setText(text)
        self.color_enabled.setChecked(True)
        self.emit_changed()

    def hex_changed(self):
        text = self.hex.text().strip()
        color = QColor(text)
        if len(text) == 7 and text.startswith('#') and color.isValid():
            self.wheel.set_color(text)
            self.color_changed(color.name())
        else:
            self.hex.setText(self.wheel.color().name())

    def state(self):
        ratio = self.ratio.currentData()
        return dict(color=self.wheel.color().name() if self.color_enabled.isChecked() else None,
                    tolerance=self.tolerance.value(), coverage=self.coverage.value()/100,
                    brightness_min=self.brightness_min.value(), brightness_max=self.brightness_max.value(),
                    orientation=self.orientation.currentData(), ratio=self.ratio_value.value() if ratio == -1 else ratio,
                    **{key: spin.value() for key, spin in self.dimensions.items()})

    def restore(self, state):
        self.restoring = True
        color = state.get('color')
        self.color_enabled.setChecked(bool(color))
        if color:
            self.wheel.set_color(color)
            self.hex.setText(color)
        self.tolerance.setValue(state.get('tolerance', 30))
        self.coverage.setValue(round(state.get('coverage', .1)*100))
        self.brightness_min.setValue(state.get('brightness_min', 0))
        self.brightness_max.setValue(state.get('brightness_max', 100))
        self.orientation.setCurrentIndex(max(0, self.orientation.findData(state.get('orientation', 'any'))))
        ratio = state.get('ratio', 0)
        index = self.ratio.findData(ratio)
        self.ratio.setCurrentIndex(index if index >= 0 else self.ratio.findData(-1))
        if index < 0:
            self.ratio_value.setValue(ratio)
        for key, spin in self.dimensions.items():
            spin.setValue(state.get(key, 0))
        self.restoring = False
        self.update_readout()
