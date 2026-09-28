from unittest import TestCase
from unittest.mock import patch
from PySide6.QtCore import Qt, QPoint, QPointF, QEvent, QRect, QSize
from PySide6.QtGui import QFocusEvent, QContextMenuEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QMenu
from snipboard.qt_board import BoardWindow
import test_qt


class BackgroundTests(TestCase):
    setUp = test_qt.QtFlowTests.setUp
    tearDown = test_qt.QtFlowTests.tearDown
    board_mouse = test_qt.QtFlowTests.board_mouse

    def board(self):
        board = self.controller.show_board()
        test_qt.drain(self.controller)
        board.setGeometry(100, 100, 700, 500)
        board.scale(.2, .2)
        test_qt.drain(self.controller)
        self.assertIsNone(board.itemAt(QPoint(20, 20)))
        return board

    def pixel(self, board, point=QPoint(20, 20)):
        image = board.viewport().grab().toImage()
        ratio = image.devicePixelRatio()
        return image.pixelColor(round(point.x() * ratio), round(point.y() * ratio))

    def test_four_backgrounds_render_without_changing_image_opacity_and_persist(self):
        board = self.board()
        handle = board.winId()
        for mode, expected in [('white', '#ffffff'), ('gray', '#808080'), ('black', '#000000')]:
            board.background_actions[mode].trigger()
            self.assertEqual(self.pixel(board).name(), expected)
            self.assertEqual(self.pixel(board).alpha(), 255)
        board.background_actions['transparent'].trigger()
        self.assertLessEqual(self.pixel(board).alpha(), 1)
        self.assertTrue(board.testAttribute(Qt.WA_TranslucentBackground))
        self.assertEqual(board.winId(), handle)
        self.assertEqual(board.windowOpacity(), 1)
        picture = next(iter(board.pictures.values()))
        point = board.mapFromScene(picture.mapToScene(picture.rect().center()))
        self.assertEqual(self.pixel(board, point).alpha(), 255)
        self.assertEqual(self.controller.catalog.get_setting('board.background'), 'transparent')
        restored = BoardWindow(self.controller)
        self.assertEqual(restored.background_mode, 'transparent')
        self.assertTrue(restored.background_actions['transparent'].isChecked())
        restored.layer.timer.stop()
        restored.deleteLater()
        test_qt.APP.processEvents()

    def test_transparent_image_movement_clears_previous_pixels(self):
        board = self.board()
        board.set_background('transparent')
        picture = next(iter(board.pictures.values()))
        point = board.mapFromScene(picture.mapToScene(picture.rect().center()))
        self.assertEqual(self.pixel(board, point).alpha(), 255)
        picture.moveBy(20000, 20000)
        test_qt.APP.processEvents()
        self.assertLessEqual(self.pixel(board, point).alpha(), 1)

    def test_transparent_move_resize_glows_while_held_and_clears_after_release(self):
        board = self.board()
        board.set_background('transparent')
        original = QRect(board.geometry())
        for local, delta, edges in [(QPoint(20, 20), QPointF(25, 20), False),
                                    (QPoint(board.width() - 2, board.height() - 2), QPointF(35, 25), True)]:
            start = QPointF(board.viewport().mapToGlobal(local))
            self.board_mouse(board, QEvent.MouseButtonPress, start, Qt.LeftButton, Qt.LeftButton)
            self.assertTrue(board.glow_active())
            self.assertGreater(self.pixel(board, QPoint(1, board.height() // 2)).alpha(), 150)
            self.board_mouse(board, QEvent.MouseMove, start + delta, buttons=Qt.LeftButton)
            QTest.qWait(550)
            self.assertTrue(board.glow_active(), 'Glow vanished while pointer was still held')
            self.board_mouse(board, QEvent.MouseButtonRelease, start + delta, Qt.LeftButton)
            QTest.qWait(600)
            self.assertFalse(board.glow_active())
            self.assertLessEqual(self.pixel(board, QPoint(1, board.height() // 2)).alpha(), 1)
        self.assertEqual(board.geometry().topLeft(), original.topLeft() + QPoint(25, 20))
        self.assertEqual(board.size(), original.size() + QSize(35, 25))

    def test_glow_cancel_focus_loss_and_opaque_modes(self):
        board = self.board()
        board.set_background('transparent')
        original = QRect(board.geometry())
        start = QPointF(board.viewport().mapToGlobal(QPoint(20, 20)))
        board.begin_window_drag(start)
        board.drag_window(start + QPointF(10, 10))
        QTest.keyClick(board, Qt.Key_Escape)
        self.assertEqual(board.geometry(), original)
        self.assertFalse(board.glow_active())
        board.arm_window_tool('resize')
        self.assertTrue(board.glow_active())
        test_qt.APP.sendEvent(board, QFocusEvent(QEvent.FocusOut))
        self.assertFalse(board.glow_active())
        board.arm_window_tool('move')
        board.set_background('white')
        self.assertFalse(board.glow_active())
        self.assertEqual(self.pixel(board).name(), '#ffffff')
        board.set_background('transparent')
        board.close()
        self.assertFalse(board.glow_active())

    def test_right_click_exposes_background_menu_and_checked_selection(self):
        board = self.board()
        board.set_background('gray')
        seen = []
        class InspectMenu(QMenu):
            def exec(menu, *_):
                submenu = next(action.menu() for action in menu.actions() if action.text() == '背景')
                seen.extend((action.text(), action.isChecked()) for action in submenu.actions())
        with patch('snipboard.qt_board.QMenu', InspectMenu):
            point = QPoint(20, 20)
            event = QContextMenuEvent(QContextMenuEvent.Mouse, point, board.viewport().mapToGlobal(point))
            board.contextMenuEvent(event)
        self.assertEqual(seen, [('黑色背景', False), ('白色背景', False), ('灰色背景', True), ('透明背景', False)])
