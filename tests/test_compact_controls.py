from unittest import TestCase
from unittest.mock import patch
import threading

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QDialog, QSpinBox

import test_qt


class CompactControlsTests(TestCase):
    setUp = test_qt.QtFlowTests.setUp
    tearDown = test_qt.QtFlowTests.tearDown

    def test_existing_library_uses_narrow_compact_default(self):
        self.controller.catalog.set_setting('board.gap', 18)
        board = self.controller.show_board()
        test_qt.drain(self.controller)
        self.assertEqual(board.compact_gap, 3)
        self.assertEqual(board.gap, 18)
        self.assertEqual(board.COMPACT_MARGIN, 3)

    def test_current_work_group_initial_layout_is_not_cancelled_by_reopening(self):
        rows = self.controller.catalog.query()
        group = self.controller.catalog.create_work_group('Current', [row['digest'] for row in rows])
        self.controller.catalog.set_current_work_group(group)
        board = self.controller.show_board()
        # Repeated hotkey presses before the worker finishes must not save the
        # temporary grid and cancel the initial compact arrangement.
        self.controller.show_board()
        test_qt.drain(self.controller)
        self.assertEqual(board.board_id, f'work:{group}')
        self.assertIsNotNone(board.arrange_result)
        self.assertEqual(set(board.arrange_result['positions']), set(board.pictures))
        with patch.object(board, 'load_group', wraps=board.load_group) as load:
            self.controller.show_board()
            load.assert_not_called()

    def test_zero_spacing_is_saved_and_rearranges_immediately(self):
        board = self.controller.show_board()
        test_qt.drain(self.controller)
        def accept():
            dialog = QApplication.activeModalWidget()
            self.assertIsInstance(dialog, QDialog)
            dialog.findChild(QSpinBox, 'compactGap').setValue(0)
            dialog.accept()
        with patch.object(board, 'arrange', wraps=board.arrange) as arrange:
            QTimer.singleShot(0, accept)
            board.appearance()
            arrange.assert_called_once_with('optimal')
        test_qt.drain(self.controller)
        self.assertEqual(board.compact_gap, 0)
        self.assertEqual(self.controller.catalog.get_setting('board.compact_gap'), 0)
        self.assertEqual(board.gap, 18)
        self.assertIsNotNone(board.arrange_result['positions'])

    def test_cancel_does_not_change_spacing_or_layout(self):
        board = self.controller.show_board()
        test_qt.drain(self.controller)
        before = board._snapshot()
        def reject():
            dialog = QApplication.activeModalWidget()
            dialog.findChild(QSpinBox, 'compactGap').setValue(30)
            dialog.reject()
        QTimer.singleShot(0, reject)
        board.appearance()
        self.assertEqual(board.compact_gap, 3)
        self.assertEqual(board._snapshot(), before)

    def test_view_commands_cancel_inflight_arrangement(self):
        from snipboard.packing import compact
        board = self.controller.show_board()
        test_qt.drain(self.controller)
        for operation in (board.fit, board.actual):
            release, started = threading.Event(), threading.Event()
            def slow(*args, **kwargs):
                started.set()
                release.wait(2)
                return compact(*args, **kwargs)
            with patch('snipboard.qt_board.compact', side_effect=slow):
                board.arrange('optimal')
                test_qt.APP.processEvents()
                self.assertTrue(started.wait(1))
                operation()
                before = board.transform(), board.mapToScene(board.viewport().rect().center()), board._snapshot()
                release.set()
                test_qt.drain(self.controller)
            self.assertEqual((board.transform(), board.mapToScene(board.viewport().rect().center()), board._snapshot()),before)
