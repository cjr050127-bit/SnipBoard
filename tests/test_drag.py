"""Exercise drag initiation from real configured photo grids, not just drop receivers."""
from unittest import TestCase
from unittest.mock import patch
from PySide6.QtCore import Qt, QPointF, QEvent, QPoint, QItemSelectionModel
from PySide6.QtGui import QMouseEvent, QDragEnterEvent, QDragMoveEvent, QDropEvent
from PySide6.QtWidgets import QListView
import test_qt
from snipboard.qt_collections import decode_drop


class DragTests(TestCase):
    setUp = test_qt.QtFlowTests.setUp
    tearDown = test_qt.QtFlowTests.tearDown

    def send_mouse(self, grid, kind, point, button=Qt.NoButton, buttons=Qt.NoButton):
        pos = QPointF(point)
        event = QMouseEvent(kind, pos, QPointF(grid.viewport().mapToGlobal(point)), button, buttons, Qt.NoModifier)
        test_qt.APP.sendEvent(grid.viewport(), event)

    def start_drag(self, grid, index, execute):
        start = grid.visualRect(index).center()
        end = start + test_qt.QPoint(45, 12)
        with patch('snipboard.qt_collections.QDrag.exec', execute):
            self.send_mouse(grid, QEvent.MouseButtonPress, start, Qt.LeftButton, Qt.LeftButton)
            self.send_mouse(grid, QEvent.MouseMove, start + test_qt.QPoint(2, 1), buttons=Qt.LeftButton)
            self.send_mouse(grid, QEvent.MouseMove, end, buttons=Qt.LeftButton)
        self.send_mouse(grid, QEvent.MouseButtonRelease, end, Qt.LeftButton)

    def test_press_move_starts_drag_with_preview_and_no_detail_activation(self):
        grid = self.window.grid
        seen = []
        def execute(drag, *args):
            seen.append((decode_drop(drag.mimeData()), drag.pixmap(), drag.hotSpot()))
            return Qt.IgnoreAction
        index = grid.model().index(0)
        digest = index.data(Qt.UserRole)['digest']
        self.start_drag(grid, index, execute)
        self.assertEqual(len(seen), 1, 'Press-and-move never entered the native drag operation')
        self.assertEqual(seen[0][0], [digest])
        self.assertFalse(seen[0][1].isNull(), 'No small image follows the mouse')
        self.assertLessEqual(seen[0][1].deviceIndependentSize().width(), 110)
        self.assertEqual(self.window.stack.currentIndex(), 0, 'Drag incorrectly opened inline detail')
        self.assertEqual(grid.dragDropMode(), QListView.DragOnly)

    def test_recommendation_grid_also_starts_drag(self):
        self.window.show_photo(self.window.rows[0])
        test_qt.drain(self.controller)
        original = self.window.current['digest']
        grid = self.window.rec_views[0]
        seen = []
        def execute(drag, *args):
            seen.append(decode_drop(drag.mimeData()))
            return Qt.IgnoreAction
        self.start_drag(grid, grid.model().index(0), execute)
        self.assertEqual(len(seen), 1)
        self.assertEqual(self.window.current['digest'], original)

    def bridge_drop(self, target, point, record):
        # Replace only the blocking OS drag loop. The actual press/move, MIME,
        # preview and enter/move/drop handlers run through production code.
        def execute(drag, *args):
            record.append(decode_drop(drag.mimeData()))
            enter = QDragEnterEvent(point, Qt.CopyAction, drag.mimeData(), Qt.LeftButton, Qt.NoModifier)
            test_qt.APP.sendEvent(target, enter)
            self.assertTrue(enter.isAccepted())
            move = QDragMoveEvent(point, Qt.CopyAction, drag.mimeData(), Qt.LeftButton, Qt.NoModifier)
            test_qt.APP.sendEvent(target, move)
            self.assertTrue(move.isAccepted())
            event = QDropEvent(QPointF(point), Qt.CopyAction, drag.mimeData(), Qt.LeftButton, Qt.NoModifier)
            test_qt.APP.sendEvent(target, event)
            self.assertTrue(event.isAccepted())
            return Qt.CopyAction
        return execute

    def test_press_move_release_creates_group_then_adds_to_existing(self):
        grid = self.window.grid
        first = grid.model().index(0)
        digest = first.data(Qt.UserRole)['digest']
        record = []
        self.start_drag(grid, first, self.bridge_drop(self.window.new_group_drop, QPoint(20, 20), record))
        test_qt.drain(self.controller)
        groups = self.controller.catalog.work_groups()
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]['count'], 1)
        self.assertEqual(record, [[digest]])
        self.assertEqual(self.window.stack.currentIndex(), 0)
        second = grid.model().index(1)
        target = self.window.work_list
        point = target.visualItemRect(target.item(0)).center()
        self.start_drag(grid, second, self.bridge_drop(target.viewport(), point, record))
        test_qt.drain(self.controller)
        self.assertEqual(self.controller.catalog.work_groups()[0]['count'], 2)
        self.assertEqual(len(self.window.rows), 8)

    def test_drag_preview_and_payload_include_multi_selection(self):
        grid = self.window.grid
        for i in (0, 1):
            grid.selectionModel().select(grid.model().index(i), QItemSelectionModel.Select)
        captured = []
        def execute(drag, *args):
            captured.extend(decode_drop(drag.mimeData()))
            self.assertFalse(drag.pixmap().isNull())
            return Qt.IgnoreAction
        self.start_drag(grid, grid.model().index(0), execute)
        self.assertEqual(len(captured), 2)
        self.assertEqual(self.controller.catalog.work_groups(), [])

    def label_point(self, reference):
        target = self.window.groups
        item = next(target.item(i) for i in range(target.count()) if target.item(i).data(Qt.UserRole) == reference)
        target.scrollToItem(item)
        return target.visualItemRect(item).center()

    def test_drag_multiple_images_to_existing_label_preserves_other_tags_and_filter(self):
        catalog = self.controller.catalog
        digests = [r['digest'] for r in self.window.rows[:3]]
        catalog.set_tags(digests[2], ['目标'])
        catalog.set_tags(digests[0], ['原有'])
        self.window.update_groups()
        reference = next(g['id'] for g in catalog.label_rows() if g['name'] == '目标')
        grid = self.window.grid
        for i in (0, 1):
            grid.selectionModel().select(grid.model().index(i), QItemSelectionModel.Select)
        self.start_drag(grid, grid.model().index(0), self.bridge_drop(self.window.groups.viewport(), self.label_point(reference), []))
        test_qt.drain(self.controller)
        self.assertEqual(catalog.tags_for(digests[0]), ['原有', '目标'])
        self.assertEqual({r['digest'] for r in catalog.collection_query(reference)}, set(digests))
        self.assertIsNone(self.window.selected_groups)
        self.assertEqual(len(self.window.rows), 8)
        self.assertEqual(self.window.stack.currentIndex(), 0)
        self.assertEqual(catalog.add_to_label(reference, digests), 0)

    def test_recommendation_drag_to_source_label_updates_count_and_viewer(self):
        catalog = self.controller.catalog
        self.window.show_photo(self.window.rows[0])
        test_qt.drain(self.controller)
        grid = self.window.rec_views[0]
        index = grid.model().index(0)
        digest = index.data(Qt.UserRole)['digest']
        reference = next(g['id'] for g in catalog.label_rows() if digest not in catalog._members(g['id']))
        before = len(catalog.collection_query(reference))
        self.start_drag(grid, index, self.bridge_drop(self.window.groups.viewport(), self.label_point(reference), []))
        test_qt.drain(self.controller)
        self.assertEqual(len(catalog.collection_query(reference)), before + 1)
        self.assertEqual(next(g['count'] for g in catalog.label_rows() if g['id'] == reference), before + 1)
        self.assertEqual(self.window.stack.currentIndex(), 1)
        self.assertIsNone(self.window.selected_groups)

    def test_label_blank_space_rejects_drop_without_creating_group_or_label(self):
        from PySide6.QtCore import QMimeData
        from snipboard.qt_collections import MIME
        import json
        mime = QMimeData()
        mime.setData(MIME, json.dumps([self.window.rows[0]['digest']]).encode())
        target = self.window.groups
        point = QPoint(10, target.viewport().height() - 5)
        self.assertIsNone(target.itemAt(point))
        before = self.controller.catalog.label_rows()
        enter = QDragEnterEvent(point, Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier)
        test_qt.APP.sendEvent(target.viewport(), enter)
        move = QDragMoveEvent(point, Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier)
        test_qt.APP.sendEvent(target.viewport(), move)
        self.assertFalse(move.isAccepted())
        drop = QDropEvent(QPointF(point), Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier)
        test_qt.APP.sendEvent(target.viewport(), drop)
        self.assertFalse(drop.isAccepted())
        self.assertEqual(self.controller.catalog.label_rows(), before)
        self.assertEqual(self.controller.catalog.work_groups(), [])

    def test_small_pointer_jitter_still_opens_inline_on_release(self):
        grid = self.window.grid
        index = grid.model().index(0)
        point = grid.visualRect(index).center()
        with patch('snipboard.qt_collections.QDrag.exec') as execute:
            self.send_mouse(grid, QEvent.MouseButtonPress, point, Qt.LeftButton, Qt.LeftButton)
            self.send_mouse(grid, QEvent.MouseMove, point + QPoint(1, 1), buttons=Qt.LeftButton)
            self.send_mouse(grid, QEvent.MouseButtonRelease, point + QPoint(1, 1), Qt.LeftButton)
            execute.assert_not_called()
        self.assertEqual(self.window.stack.currentIndex(), 1)
