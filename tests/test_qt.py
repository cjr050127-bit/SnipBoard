import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from pathlib import Path
import tempfile
import time
import sys
import traceback
import math
import json
from unittest.mock import patch
import unittest
from PIL import Image
from PySide6.QtCore import Qt, QPoint, QPointF, QEvent, QRect, QMimeData, QItemSelectionModel
from PySide6.QtGui import QMouseEvent, QWheelEvent, QDragEnterEvent, QDropEvent, QContextMenuEvent
from PySide6.QtWidgets import QApplication, QMenu
from PySide6.QtTest import QTest

from snipboard.qt_app import Controller, configure_app
from snipboard.qt_support import STYLE
from snipboard.qt_collections import MIME

APP = QApplication.instance() or QApplication([])
configure_app(APP)


def drain(controller, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        APP.processEvents()
        QTest.qWait(15)
        if not controller.jobs.pending:
            APP.processEvents()
            if not controller.jobs.pending:
                return
    raise AssertionError('Background jobs did not finish')


class QtFlowTests(unittest.TestCase):
    def setUp(self):
        self.callback_errors = []
        self.previous_hook = sys.excepthook
        sys.excepthook = lambda *args: self.callback_errors.append(''.join(traceback.format_exception(*args)))
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / 'source'
        for group, color in [('ABC123', '#e34242'), ('DEF456', '#4385db')]:
            folder = self.source / 'history' / group
            folder.mkdir(parents=True)
            for index in range(4):
                Image.new('RGB', (180 + index * 11, 120 + index * 9), color).save(folder / f'image{index}.png')
        self.controller = Controller(APP, self.source, self.root / 'library', native=False)
        self.controller.timer.stop()
        self.controller.catalog.sync_source(self.source)
        self.window = self.controller.new_library()
        drain(self.controller)

    def tearDown(self):
        self.controller.quit()
        drain(self.controller)
        self.controller._finish_shutdown()
        APP.processEvents()
        self.temp.cleanup()
        sys.excepthook = self.previous_hook
        self.assertEqual(self.callback_errors, [], '\n'.join(self.callback_errors))

    def test_click_is_inline_and_recommendations_have_two_rows(self):
        self.assertEqual(self.window.model.rowCount(), 8)
        index = self.window.model.index(0)
        QTest.mouseClick(self.window.grid.viewport(), Qt.LeftButton, pos=self.window.grid.visualRect(index).center())
        drain(self.controller)
        self.assertEqual(self.window.stack.currentIndex(), 1)
        self.assertEqual(len(self.controller.windows), 1)
        self.assertEqual(len(self.window.rec_views), 2)
        self.assertIsNotNone(self.window.detail_view.image_item)
        recommended = self.window.rec_models[0].rows
        self.assertTrue(recommended)
        first = self.window.current['digest']
        self.window.show_photo(recommended[0])
        drain(self.controller)
        self.assertNotEqual(first, self.window.current['digest'])
        self.window.return_to_grid()
        self.assertEqual(self.window.stack.currentIndex(), 0)

    def test_independent_library_windows_and_filter_intersection(self):
        other = self.controller.new_library()
        drain(self.controller)
        group = next(g['id'] for g in self.controller.catalog.group_rows() if g['group_id'] == 'ABC123')
        self.window.selected_groups = [group]
        self.window.colors = ['#4385db']
        self.window.refresh()
        drain(self.controller)
        self.assertEqual(len(self.window.rows), 0)
        self.assertEqual(len(other.rows), 8)

    def test_group_name_single_click_and_unsigned_hash_survive_qt(self):
        item = self.window.groups.item(0)
        rect = self.window.groups.visualItemRect(item)
        QTest.mouseClick(self.window.groups.viewport(), Qt.LeftButton, pos=QPoint(75, rect.center().y()))
        drain(self.controller)
        self.assertEqual(len(self.window.rows), 4)
        self.window.rows[0]['feature']['hash'] = str((1 << 64) - 1)
        value = self.window.model.index(0).data(Qt.UserRole)
        self.assertEqual(value['feature']['hash'], '18446744073709551615')

    def test_board_group_shortcuts_layout_and_undo(self):
        board = self.controller.show_board()
        drain(self.controller)
        first_group = board.board_id
        self.assertEqual(board.backgroundBrush().color().name(), '#000000')
        self.assertTrue(board.windowFlags() & Qt.FramelessWindowHint)
        self.assertEqual(len(board.pictures), 4)
        before = board._snapshot()
        board.arrange('random')
        board.align('left')
        board.undo(-1)
        board.undo(-1)
        self.assertEqual(board._snapshot(), before)
        QTest.keyClick(board, Qt.Key_PageDown)
        drain(self.controller)
        self.assertNotEqual(board.board_id, first_group)
        last = board.board_id
        board.close()
        self.controller.show_board()
        self.assertEqual(board.board_id, last)
        board.save()
        self.assertIsNotNone(self.controller.catalog.layout(last))
        self.assertEqual(self.controller.catalog.get_setting('board.last_group'), last)

    def test_closing_one_library_during_query_does_not_stop_other(self):
        second = self.controller.new_library()
        self.window.refresh()
        self.window.close()
        drain(self.controller)
        self.assertEqual(self.controller.windows, [second])
        self.assertFalse(self.controller.quitting)

    def test_rotation_preserves_centre_undo_and_saved_group(self):
        board = self.controller.show_board()
        drain(self.controller)
        picture = next(iter(board.pictures.values()))
        digest = picture.row['digest']
        before = board._snapshot()
        center = picture.mapToScene(picture.rect().center())
        picture.setSelected(True)
        board.commands['顺时针旋转 90°'].trigger()
        self.assertEqual(picture.rotation(), 90)
        self.assertLess((picture.mapToScene(picture.rect().center()) - center).manhattanLength(), .001)
        self.assertAlmostEqual(picture.sceneBoundingRect().width(), picture.rect().height())
        board.undo(-1)
        self.assertEqual(board._snapshot(), before)
        board.undo(1)
        board.save()
        group = board.board_id
        board.change_group(1)
        board.load_group(group)
        self.assertEqual(board.pictures[digest].rotation(), 90)
        board.pictures[digest].setSelected(True)
        board.rotate(-22.5)
        self.assertEqual(board.pictures[digest].rotation(), 67.5)
        board.rotate(0, absolute=True)
        self.assertEqual(board.pictures[digest].rotation(), 0)

    def test_rotated_images_arrange_align_distribute_without_overlap(self):
        board = self.controller.show_board()
        drain(self.controller)
        pictures = list(board.pictures.values())
        for picture, angle in zip(pictures, (90, 15, 135, 270)):
            picture.setSelected(True)
            picture.setRotation(angle)
        for mode in ('optimal', 'addition', 'name', 'order', 'path', 'random'):
            board.arrange(mode)
            drain(self.controller)
            for i, picture in enumerate(pictures):
                for other in pictures[i + 1:]:
                    self.assertFalse(picture.sceneBoundingRect().intersects(other.sceneBoundingRect()), mode)
        board.align('top')
        tops = [p.sceneBoundingRect().top() for p in pictures]
        self.assertLess(max(tops) - min(tops), .001)
        board.distribute(True)
        bounds = sorted([p.sceneBoundingRect() for p in pictures], key=lambda b: b.left())
        gaps = [b.left() - a.right() for a, b in zip(bounds, bounds[1:])]
        self.assertGreaterEqual(min(gaps), -.001)
        self.assertLess(max(gaps) - min(gaps), .001)
        picture = pictures[0]
        anchor = picture.mapToScene(QPointF())
        picture.set_width(400)
        self.assertLess((picture.mapToScene(QPointF()) - anchor).manhattanLength(), .001)
        self.assertAlmostEqual(picture.rect().height() / picture.rect().width(), picture.row['height'] / picture.row['width'])

    def board_mouse(self, board, kind, global_pos, button=Qt.NoButton, buttons=Qt.NoButton, modifiers=Qt.NoModifier):
        local = QPointF(board.viewport().mapFromGlobal(global_pos.toPoint()))
        event = QMouseEvent(kind, local, global_pos, button, buttons, modifiers)
        APP.sendEvent(board.viewport(), event)

    def test_window_drag_resize_cancel_and_geometry_persistence(self):
        board = self.controller.show_board()
        drain(self.controller)
        board.setGeometry(100, 100, 700, 500)
        original = QRect(board.geometry())
        pos = QPointF(board.viewport().mapToGlobal(QPoint(500, 350)))
        self.board_mouse(board, QEvent.MouseButtonPress, pos, Qt.LeftButton, Qt.LeftButton, Qt.AltModifier)
        end = pos + QPointF(40, 30)
        self.board_mouse(board, QEvent.MouseMove, end, buttons=Qt.LeftButton, modifiers=Qt.AltModifier)
        self.board_mouse(board, QEvent.MouseButtonRelease, end, Qt.LeftButton, modifiers=Qt.AltModifier)
        self.assertEqual(board.geometry(), original.translated(40, 30))
        pos = QPointF(board.viewport().mapToGlobal(QPoint(500, 350)))
        self.board_mouse(board, QEvent.MouseButtonPress, pos, Qt.RightButton, Qt.RightButton, Qt.AltModifier)
        end = pos + QPointF(100, 70)
        self.board_mouse(board, QEvent.MouseMove, end, buttons=Qt.RightButton, modifiers=Qt.AltModifier)
        self.board_mouse(board, QEvent.MouseButtonRelease, end, Qt.RightButton, modifiers=Qt.AltModifier)
        self.assertEqual((board.width(), board.height()), (800, 570))
        self.assertIsNone(board.window_drag)
        board.save()
        self.assertEqual(self.controller.catalog.get_setting('board.geometry'), [140, 130, 800, 570])
        before = QRect(board.geometry())
        board.begin_window_drag(pos, Qt.LeftEdge | Qt.TopEdge)
        board.drag_window(pos + QPointF(3000, 3000))
        self.assertEqual(board.size(), board.minimumSize())
        QTest.keyClick(board, Qt.Key_Escape)
        self.assertEqual(board.geometry(), before)
        board.arm_window_tool('resize')
        QTest.keyClick(board, Qt.Key_Escape)
        self.assertIsNone(board.window_tool)

    def test_window_edges_blank_drag_and_presets(self):
        board = self.controller.show_board()
        drain(self.controller)
        board.resize(700, 500)
        self.assertEqual(board.edge_at(QPointF(1, 1)), Qt.LeftEdge | Qt.TopEdge)
        QTest.mouseMove(board.viewport(), QPoint(1, 1))
        self.assertEqual(board.viewport().cursor().shape(), Qt.SizeFDiagCursor)
        board.place_window('left')
        area = board.screen().availableGeometry()
        self.assertEqual(board.geometry().left(), area.left())
        self.assertEqual(board.width(), area.width() // 2)
        board.place_window('right')
        self.assertEqual(board.geometry().right(), area.right())
        board.resize(400, 300)
        board.place_window('center')
        self.assertEqual(board.geometry().center(), area.center())
        board.scale(.1, .1)
        local = QPoint(20, 20)
        self.assertIsNone(board.itemAt(local))
        pos = QPointF(board.viewport().mapToGlobal(local))
        before = QRect(board.geometry())
        self.board_mouse(board, QEvent.MouseButtonPress, pos, Qt.LeftButton, Qt.LeftButton)
        self.assertIsNotNone(board.window_drag)
        self.board_mouse(board, QEvent.MouseButtonRelease, pos + QPointF(20, 10), Qt.LeftButton)
        self.assertEqual(board.geometry(), before.translated(20, 10))
        pos = QPointF(board.viewport().mapToGlobal(local))
        self.board_mouse(board, QEvent.MouseButtonPress, pos, Qt.LeftButton, Qt.LeftButton, Qt.ShiftModifier)
        self.assertIsNone(board.window_drag)
        self.board_mouse(board, QEvent.MouseButtonRelease, pos, Qt.LeftButton, modifiers=Qt.ShiftModifier)

    def test_legacy_layout_without_angles_and_rotation_selection_scope(self):
        board = self.controller.show_board()
        drain(self.controller)
        snapshot = board._snapshot()
        for record in snapshot.values():
            del record['angle']
        group = board.board_id
        board.change_group(1)
        self.controller.catalog.save_layout(group, {'items': snapshot})
        board.load_group(group)
        self.assertTrue(all(p.rotation() == 0 for p in board.pictures.values()))
        board.rotate(90)
        self.assertTrue(all(p.rotation() == 0 for p in board.pictures.values()))
        selected = list(board.pictures.values())[:2]
        for p in selected: p.setSelected(True)
        board.rotate(90)
        self.assertEqual(sum(p.rotation() == 90 for p in board.pictures.values()), 2)

    def test_rotation_keys_and_wheel_leave_view_zoom_unchanged(self):
        board = self.controller.show_board()
        drain(self.controller)
        picture = next(iter(board.pictures.values()))
        picture.setSelected(True)
        QTest.keyClick(board, Qt.Key_R)
        self.assertEqual(picture.rotation(), 90)
        QTest.keyClick(board, Qt.Key_R, Qt.ShiftModifier)
        self.assertEqual(picture.rotation(), 0)
        zoom = board.transform().m11()
        for modifiers, expected in [(Qt.ShiftModifier, 15), (Qt.ShiftModifier | Qt.ControlModifier, 16)]:
            event = QWheelEvent(QPointF(200, 200), QPointF(400, 400), QPoint(), QPoint(0, 120),
                                Qt.NoButton, modifiers, Qt.NoScrollPhase, False)
            APP.sendEvent(board.viewport(), event)
            self.assertEqual(picture.rotation(), expected)
            self.assertEqual(board.transform().m11(), zoom)
        QTest.keyClick(board, Qt.Key_R, Qt.ControlModifier)
        self.assertEqual(picture.rotation(), 0)

    def test_corner_hover_drag_rotates_and_undoes(self):
        board = self.controller.show_board()
        drain(self.controller)
        picture = next(iter(board.pictures.values()))
        center = picture.mapToScene(picture.rect().center())
        local = picture.rect().bottomRight() - QPointF(3, 3)
        start_scene = picture.mapToScene(local)
        start_view = board.mapFromScene(start_scene)
        start = QPointF(board.viewport().mapToGlobal(start_view))
        self.board_mouse(board, QEvent.MouseMove, start)
        self.assertEqual(picture.cursor().shape(), Qt.BitmapCursor,
                         str((start_view, board.itemAt(start_view), picture.corner_mode(local))))
        self.board_mouse(board, QEvent.MouseButtonPress, start, Qt.LeftButton, Qt.LeftButton)
        self.assertTrue(picture.rotating)
        vector = start_scene - center
        angle = math.radians(40)
        end_scene = center + QPointF(vector.x()*math.cos(angle)-vector.y()*math.sin(angle),
                                    vector.x()*math.sin(angle)+vector.y()*math.cos(angle))
        end = QPointF(board.viewport().mapToGlobal(board.mapFromScene(end_scene)))
        self.board_mouse(board, QEvent.MouseMove, end, buttons=Qt.LeftButton)
        self.board_mouse(board, QEvent.MouseButtonRelease, end, Qt.LeftButton)
        self.assertAlmostEqual(picture.rotation(), 40, delta=1)
        self.assertLess((picture.mapToScene(picture.rect().center()) - center).manhattanLength(), .01)
        self.assertFalse(picture.rotating)
        board.undo(-1)
        self.assertEqual(picture.rotation(), 0)

    def test_inline_labels_work_group_priority_and_auto_sync(self):
        window = self.window
        window.show_photo(window.rows[0])
        digest = window.current['digest']
        window.tag_editor.setText('角色，暖光')
        window.save_tags()
        drain(self.controller)
        self.assertEqual(self.controller.catalog.tags_for(digest), ['暖光', '角色'])
        label = next(g for g in self.controller.catalog.label_rows() if g['name'] == '角色')
        window.select_group(label['id'])
        drain(self.controller)
        self.assertEqual([r['digest'] for r in window.rows], [digest])
        board = self.controller.show_board(label['id'])
        self.assertEqual(set(board.pictures), {digest})
        group = self.controller.catalog.create_work_group('进行中的作品')
        self.controller.catalog.set_current_work_group(group)
        board = self.controller.show_board()
        self.assertEqual(board.board_id, f'work:{group}')
        self.assertEqual(board.pictures, {})
        Image.new('RGB', (130, 210), '#876532').save(self.source / 'history' / 'ABC123' / 'new-ref.png')
        self.controller.sync()
        drain(self.controller)
        self.assertEqual(len(board.pictures), 1)
        self.assertEqual(self.controller.catalog.work_groups()[0]['count'], 1)
        self.assertIn('当前：进行中的作品', window.current_work_label.text())
        board.load_group(label['id'])
        self.controller.show_board()
        self.assertEqual(board.board_id, f'work:{group}')

    def send_drop(self, widget, digests, position=QPoint(20, 20)):
        mime = QMimeData()
        mime.setData(MIME, json.dumps(digests).encode())
        enter = QDragEnterEvent(position, Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier)
        APP.sendEvent(widget, enter)
        self.assertTrue(enter.isAccepted())
        event = QDropEvent(QPointF(position), Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier)
        APP.sendEvent(widget, event)
        self.assertTrue(event.isAccepted())

    def test_thumbnail_drop_creates_and_adds_work_group(self):
        window = self.window
        digests = [r['digest'] for r in window.rows[:2]]
        self.send_drop(window.new_group_drop, digests)
        drain(self.controller)
        groups = self.controller.catalog.work_groups()
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]['count'], 2)
        third = window.rows[2]['digest']
        rect = window.work_list.visualItemRect(window.work_list.item(0))
        self.send_drop(window.work_list.viewport(), [third], rect.center())
        drain(self.controller)
        self.assertEqual(self.controller.catalog.work_groups()[0]['count'], 3)
        self.assertEqual(len(window.rows), 8)

    def test_filter_panel_intersection_and_window_restore(self):
        window = self.window
        row = window.rows[0]
        group = self.controller.catalog.create_work_group('一张参考', [row['digest']])
        self.controller.catalog.set_tags(row['digest'], ['目标'])
        label = next(g['id'] for g in self.controller.catalog.label_rows() if g['kind'] == 'manual')
        window.selected_groups = [label]
        window.selected_work = group
        window.filter_panel.restore(dict(orientation='portrait', min_width=170))
        window.refresh()
        drain(self.controller)
        self.assertEqual(window.rows, [])
        window.filter_panel.orientation.setCurrentIndex(window.filter_panel.orientation.findData('landscape'))
        QTest.qWait(250)
        drain(self.controller)
        self.assertEqual(len(window.rows), 1)
        second = self.controller.new_library(window.state())
        drain(self.controller)
        self.assertEqual([r['digest'] for r in second.rows], [row['digest']])
        self.assertEqual(second.selected_work, group)
        self.assertEqual(second.selected_groups, [label])

    def test_context_menu_delete_updates_all_windows_and_open_viewer(self):
        digest = self.window.rows[0]['digest']
        group = self.controller.catalog.create_work_group('临时参考', [digest])
        self.controller.catalog.set_current_work_group(group)
        second = self.controller.new_library()
        self.window.select_work(group)
        second.select_work(group)
        board = self.controller.show_board()
        drain(self.controller)
        self.assertEqual(board.board_id, f'work:{group}')
        board.save()
        position = self.window.work_list.visualItemRect(self.window.work_list.item(0)).center()
        invoked = []
        class DeleteMenu(QMenu):
            def exec(menu, *_):
                action = next(a for a in menu.actions() if a.text() == '删除参考组（保留图片）')
                invoked.append(action.text())
                action.trigger()
        with patch('snipboard.qt_library.QMenu', DeleteMenu):
            self.window.work_menu(position)
        drain(self.controller)
        self.assertEqual(len(invoked), 1)
        for window in (self.window, second):
            self.assertEqual(window.work_list.count(), 0)
            self.assertIsNone(window.selected_work)
            self.assertEqual(len(window.rows), 8)
        self.assertIsNone(self.controller.catalog.get_setting('work.current'))
        self.assertNotEqual(board.board_id, f'work:{group}')
        board.save()
        self.assertIsNone(self.controller.catalog.layout(f'work:{group}'))
        reopened = self.controller.new_library({'work_group': group})
        drain(self.controller)
        self.assertIsNone(reopened.selected_work)
        self.assertEqual(len(reopened.rows), 8)

    def test_file_menu_import_refreshes_windows_and_opens_local_collection(self):
        path = self.root / 'local.webp'
        Image.new('RGB', (88, 177), '#45b756').save(path)
        second = self.controller.new_library()
        self.window.filter_panel.restore(dict(orientation='landscape'))
        file_menu = self.window.menuBar().actions()[0].menu()
        action = next(a for a in file_menu.actions() if a.text() == '导入图片…')
        self.assertEqual(action.shortcut().toString(), 'Ctrl+O')
        with patch('snipboard.qt_library.QFileDialog') as dialog:
            dialog.getOpenFileNames.return_value = ([str(path)], '')
            action.trigger()
        drain(self.controller)
        self.assertFalse(self.controller.import_running)
        self.assertEqual(len(self.window.rows), 1)
        self.assertEqual(len(second.rows), 9)
        self.assertEqual(self.window.rows[0]['source_name'], 'local.webp')
        self.window.show_photo(self.window.rows[0])
        drain(self.controller)
        self.assertIsNotNone(self.window.detail_view.image_item)
        board = self.controller.show_board(self.window.selected_groups[0])
        self.assertEqual(len(board.pictures), 1)
        with patch('snipboard.qt_library.QFileDialog') as dialog:
            dialog.getOpenFileNames.return_value = ([], '')
            action.trigger()
        self.assertFalse(self.controller.import_running)

    def test_label_context_remove_refreshes_filters_detail_and_viewer(self):
        row = self.window.rows[0]
        source_label = row['board_id']
        self.controller.catalog.set_tags(row['digest'], ['移除此标签'])
        manual = next(g['id'] for g in self.controller.catalog.label_rows() if g['kind'] == 'manual')
        second = self.controller.new_library()
        second.select_group(source_label)
        self.window.show_photo(row)
        board = self.controller.show_board(source_label)
        self.window.update_groups()
        class RemoveMenu(QMenu):
            def exec(menu, *_):
                next(a for a in menu.actions() if a.text() == '移除此标签（保留图片）').trigger()
        for reference in [manual, source_label]:
            item = next(self.window.groups.item(i) for i in range(self.window.groups.count())
                        if self.window.groups.item(i).data(Qt.UserRole) == reference)
            pos = self.window.groups.visualItemRect(item).center()
            with patch('snipboard.qt_library.QMenu', RemoveMenu):
                self.window.groups.customContextMenuRequested.emit(pos)
            drain(self.controller)
        self.assertEqual(self.window.tag_editor.text(), '')
        self.assertEqual(self.window.current['memberships'], [])
        self.assertIsNone(second.selected_groups)
        self.assertEqual(len(second.rows), 8)
        self.assertNotEqual(board.board_id, source_label)
        self.controller.sync()
        drain(self.controller)
        self.assertNotIn(source_label, [g['id'] for g in self.controller.catalog.label_rows()])

    def test_image_context_delete_batch_recommendation_and_detail(self):
        second = self.controller.new_library()
        self.window.grid.selectionModel().clearSelection()
        targets = [self.window.rows[i]['digest'] for i in range(2)]
        for i in range(2):
            self.window.grid.selectionModel().select(self.window.model.index(i), QItemSelectionModel.Select)
        second.show_photo(self.window.rows[0])
        class DeleteMenu(QMenu):
            def exec(menu, *_):
                next(a for a in menu.actions() if a.text().startswith('从图片库删除')).trigger()
        pos = self.window.grid.visualRect(self.window.model.index(0)).center()
        with patch('snipboard.qt_library.QMenu', DeleteMenu):
            self.window.grid.customContextMenuRequested.emit(pos)
        drain(self.controller)
        self.assertEqual(len(self.window.rows), 6)
        self.assertEqual(len(second.rows), 6)
        self.assertIsNone(second.current)
        self.assertTrue(all(not self.controller.catalog.has_image(d) for d in targets))
        self.window.show_photo(self.window.rows[0])
        drain(self.controller)
        view = self.window.rec_views[0]
        recommended = view.model().index(0).data(Qt.UserRole)['digest']
        with patch('snipboard.qt_library.QMenu', DeleteMenu):
            view.customContextMenuRequested.emit(view.visualRect(view.model().index(0)).center())
        drain(self.controller)
        self.assertFalse(self.controller.catalog.has_image(recommended))
        detail_digest = self.window.current['digest']
        center = self.window.detail_view.mapFromScene(self.window.detail_view.image_item.sceneBoundingRect().center())
        with patch('snipboard.qt_library.QMenu', DeleteMenu):
            self.window.detail_view.customContextMenuRequested.emit(center)
        drain(self.controller)
        self.assertFalse(self.controller.catalog.has_image(detail_digest))
        self.assertIsNone(self.window.current)
        self.assertEqual(len(self.window.rows), 4)
        self.window.forward()
        self.assertIsNone(self.window.current)

    def test_board_context_delete_local_image_and_sync_does_not_restore_it(self):
        imported = self.controller.catalog.import_images([self.source / 'history' / 'ABC123' / 'image0.png'])
        board = self.controller.show_board(imported['board_id'])
        drain(self.controller)
        picture = next(iter(board.pictures.values()))
        digest = picture.row['digest']
        pos = board.mapFromScene(picture.sceneBoundingRect().center())
        event = QContextMenuEvent(QContextMenuEvent.Mouse, pos, board.viewport().mapToGlobal(pos))
        class DeleteMenu(QMenu):
            def exec(menu, *_):
                next(a for a in menu.actions() if a.text().startswith('从图片库删除')).trigger()
        with patch('snipboard.qt_board.QMenu', DeleteMenu):
            board.contextMenuEvent(event)
        drain(self.controller)
        self.assertEqual(board.pictures, {})
        self.assertFalse(self.controller.catalog.has_image(digest))
        self.controller.sync()
        drain(self.controller)
        self.assertEqual(board.pictures, {})
        self.assertEqual(len(self.window.rows), 7)
