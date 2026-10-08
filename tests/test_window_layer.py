import unittest
from unittest.mock import patch
from PySide6.QtCore import Qt
from PySide6.QtGui import QContextMenuEvent
from PySide6.QtWidgets import QMenu
from PySide6.QtTest import QTest
from snipboard.qt_board import BoardWindow
import test_qt

class BoardLayerTests(unittest.TestCase):
    setUp = test_qt.QtFlowTests.setUp
    tearDown = test_qt.QtFlowTests.tearDown

    def test_new_and_existing_normal_defaults_become_top_once(self):
        self.controller.catalog.set_setting('board.layer', 'normal')
        board = self.controller.show_board()
        test_qt.drain(self.controller)
        self.assertEqual(board.layer.mode, 'top')
        self.assertTrue(board.layer.timer.isActive())
        action = board.commands['始终置顶']
        self.assertTrue(action.isChecked())
        self.assertEqual(action.shortcut().toString(), 'Ctrl+Shift+A')
        QTest.keyClick(board, Qt.Key_A, Qt.ControlModifier | Qt.ShiftModifier)
        test_qt.drain(self.controller)
        self.assertEqual(board.layer.mode, 'normal')
        self.assertFalse(action.isChecked())
        self.assertFalse(board.layer.timer.isActive())
        restored = BoardWindow(self.controller)
        self.addCleanup(restored.deleteLater)
        self.assertEqual(restored.layer.mode, 'normal')
        restored.commands['始终置顶'].trigger()
        self.assertEqual(restored.layer.mode, 'top')
        restored.layer.timer.stop()

    def test_fresh_default_and_right_click_toggle_are_consistent(self):
        board = self.controller.show_board()
        self.assertEqual(board.layer.mode, 'top')
        class ToggleMenu(QMenu):
            def exec(menu, *_):
                action = next(a for a in menu.actions() if a.text() == '始终置顶')
                self.assertTrue(action.isChecked())
                action.trigger()
        point = board.viewport().rect().center()
        with patch('snipboard.qt_board.QMenu', ToggleMenu):
            board.contextMenuEvent(QContextMenuEvent(QContextMenuEvent.Mouse, point, board.viewport().mapToGlobal(point)))
        self.assertEqual(board.layer.mode, 'normal')
        board.set_layer('top')
        self.assertTrue(board.commands['始终置顶'].isChecked())
        board.set_layer('bottom')
        self.assertFalse(board.commands['始终置顶'].isChecked())
        board.set_layer('normal')

    def test_existing_explicit_bottom_mode_is_preserved(self):
        self.controller.catalog.set_setting('board.layer', 'bottom')
        board = self.controller.show_board()
        self.assertEqual(board.layer.mode, 'bottom')
        board.set_layer('normal')
