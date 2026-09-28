"""Native Qt acceptance in owned hidden windows; never drives the user's desktop."""
import ctypes
import math
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import traceback

os.environ['QT_QPA_PLATFORM'] = 'windows'
from PIL import Image, ImageDraw
from PySide6.QtCore import Qt, QPointF, QPoint, QEvent, QRect
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication, QWidget
from PySide6.QtTest import QTest
from snipboard.qt_app import Controller, configure_app
from snipboard.qt_board import BoardWindow
from snipboard.qt_library import LibraryWindow
from snipboard.qt_windows import NativeHotkey

app = QApplication([])
configure_app(app)
errors = []
def on_error(*args):
    errors.append(''.join(traceback.format_exception(*args)))
    sys.__excepthook__(*args)
sys.excepthook = on_error
output = Path(__file__).resolve().parents[1] / 'docs' / 'packing-0.5.5'
output.mkdir(exist_ok=True)
scale = os.environ.get('QT_SCALE_FACTOR', '1')
report = {'requested_scale': scale, 'platform': app.platformName(),
          'screens': [{'name': s.name(), 'dpr': s.devicePixelRatio(),
                       'available': s.availableGeometry().getRect()} for s in app.screens()]}

def send(board, kind, point, button=Qt.NoButton, buttons=Qt.NoButton, modifiers=Qt.NoModifier):
    local = QPointF(board.viewport().mapFromGlobal(point.toPoint()))
    app.sendEvent(board.viewport(), QMouseEvent(kind, local, point, button, buttons, modifiers))
    app.processEvents()

with tempfile.TemporaryDirectory(prefix='snipboard-native-') as temp:
    root = Path(temp)
    source = root / 'source' / 'history' / 'ABC123'
    source.mkdir(parents=True)
    for index, color in enumerate(('#d85848', '#477abb', '#bca44c', '#798764')):
        image = Image.new('RGB', (360 + 20 * index, 200), color)
        draw = ImageDraw.Draw(image)
        draw.rectangle((20, 20, 110, 100), fill='white')
        draw.text((145, 70), f'IMAGE {index}', fill='black', font_size=24)
        image.save(source / f'{index}.png')
    controller = Controller(app, source.parent.parent, root / 'library', native=False)
    controller.timer.stop()
    controller.catalog.sync_source(controller.source)
    rows = controller.catalog.query()
    work_group = controller.catalog.create_work_group('作品参考', [r['digest'] for r in rows[:2]])
    controller.catalog.set_tags(rows[0]['digest'], ['手动标签'])
    controller.catalog.set_current_work_group(work_group)
    def drain():
        until = time.monotonic() + 30
        while time.monotonic() < until:
            app.processEvents()
            QTest.qWait(20)
            if not controller.jobs.pending:
                app.processEvents()
                if not controller.jobs.pending:
                    return
        raise RuntimeError('Workers timed out')
    window = LibraryWindow(controller)
    controller.windows.append(window)
    window.setAttribute(Qt.WA_DontShowOnScreen)
    window.show()
    drain()
    window.show_photo(window.rows[0])
    drain()
    assert window.stack.currentIndex() == 1 and len(window.rec_views) == 2
    assert window.grab().save(str(output / f'library-scale-{scale}.png'))
    board = BoardWindow(controller)
    controller.board = board
    board.setAttribute(Qt.WA_DontShowOnScreen)
    board.update_groups()
    board.show()
    board.setGeometry(100, 100, 800, 550)
    board.fit()
    drain()
    picture = next(iter(board.pictures.values()))
    center = picture.mapToScene(picture.rect().center())
    point = picture.mapToScene(picture.rect().bottomRight() - QPointF(4, 4))
    position = QPointF(board.viewport().mapToGlobal(board.mapFromScene(point)))
    send(board, QEvent.MouseMove, position)
    assert picture.cursor().shape() == Qt.BitmapCursor
    send(board, QEvent.MouseButtonPress, position, Qt.LeftButton, Qt.LeftButton)
    assert picture.rotating
    delta = point - center
    angle = math.radians(35)
    destination = center + QPointF(delta.x()*math.cos(angle)-delta.y()*math.sin(angle),
                                  delta.x()*math.sin(angle)+delta.y()*math.cos(angle))
    position = QPointF(board.viewport().mapToGlobal(board.mapFromScene(destination)))
    send(board, QEvent.MouseMove, position, buttons=Qt.LeftButton)
    send(board, QEvent.MouseButtonRelease, position, Qt.LeftButton)
    assert abs(picture.rotation() - 35) < 1, picture.rotation()
    report['corner_cursor_and_drag_rotation'] = True
    board.rotate(0, absolute=True)
    original = QRect(board.geometry())
    pos = QPointF(board.viewport().mapToGlobal(QPoint(400, 300)))
    send(board, QEvent.MouseButtonPress, pos, Qt.LeftButton, Qt.LeftButton, Qt.AltModifier)
    send(board, QEvent.MouseMove, pos + QPointF(50, 30), buttons=Qt.LeftButton)
    send(board, QEvent.MouseButtonRelease, pos + QPointF(50, 30), Qt.LeftButton)
    assert board.geometry() == original.translated(50, 30), (board.geometry(), original)
    report['native_window_move'] = True
    pos = QPointF(board.viewport().mapToGlobal(QPoint(600, 400)))
    send(board, QEvent.MouseButtonPress, pos, Qt.RightButton, Qt.RightButton, Qt.AltModifier)
    send(board, QEvent.MouseMove, pos + QPointF(90, 60), buttons=Qt.RightButton)
    send(board, QEvent.MouseButtonRelease, pos + QPointF(90, 60), Qt.RightButton)
    assert (board.width(), board.height()) == (890, 610), board.geometry()
    report['native_window_resize'] = True
    board.save()
    geometry = controller.catalog.get_setting('board.geometry')
    assert geometry == [150, 130, 890, 610], geometry
    for picture, angle in zip(board.pictures.values(), (90, 15, 315, 0)):
        board.scene_model.clearSelection()
        picture.setSelected(True)
        board.rotate(angle)
    board.scene_model.clearSelection()
    board.arrange('optimal')
    drain()
    assert board.arrange_result and board.arrange_result['positions']
    for picture in board.pictures.values():
        bounds = board.mapFromScene(picture.sceneBoundingRect()).boundingRect()
        assert board.viewport().rect().contains(bounds), bounds
    report['fixed_viewport_compact_layout'] = True
    report['packing_occupancy'] = board.arrange_result['occupancy']
    board.save()
    expected = board._snapshot()
    board.load_group(board.board_id)
    assert board._snapshot() == expected
    board.fit()
    drain()
    assert board.grab().save(str(output / f'viewer-scale-{scale}.png'))
    report['rotated_layout_restored_and_rendered'] = True
    user = ctypes.WinDLL('user32')
    user.GetWindowLongPtrW.argtypes = (ctypes.c_void_p, ctypes.c_int)
    user.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    board.layer.set_mode('top')
    assert user.GetWindowLongPtrW(int(board.winId()), -20) & 8
    board.layer.set_mode('normal')
    assert not user.GetWindowLongPtrW(int(board.winId()), -20) & 8
    target = QWidget()
    target.setAttribute(Qt.WA_DontShowOnScreen)
    target.show()
    board.layer.set_mode('application', int(target.winId()))
    target.destroy()
    board.layer.refresh()
    assert board.layer.mode == 'normal' and not board.layer.timer.isActive()
    report['target_closed_restores_normal_layer'] = True
    hotkey = NativeHotkey(app, lambda: None)
    second = NativeHotkey(app, lambda: None)
    report['hotkey_registered'] = hotkey.registered
    report['hotkey_conflict_reported'] = not second.registered
    assert not second.registered
    second.close()
    hotkey.close()
    report['topmost_on_off'] = True
    controller.quit()
    drain()
    controller._finish_shutdown()
    app.processEvents()
assert not errors, errors
report['callback_errors'] = errors
(output / f'native-scale-{scale}.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps(report, ensure_ascii=True))
