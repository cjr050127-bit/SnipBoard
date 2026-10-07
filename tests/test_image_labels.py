from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from PIL import Image
from PySide6.QtCore import Qt, QItemSelectionModel
from PySide6.QtWidgets import QMenu
from PySide6.QtTest import QTest
from snipboard.catalog import Catalog
import test_qt


class SingleImageLabelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source'
        folder = self.source / 'history' / 'ABC123'
        folder.mkdir(parents=True)
        self.paths = [folder / 'red.png', folder / 'blue.png']
        for path, color in zip(self.paths, ['red', 'blue']):
            Image.new('RGB', (80, 60), color).save(path)
        self.c = Catalog(self.root / 'library')
        self.addCleanup(self.c.close)
        self.c.sync_source(self.source)
        self.row, self.other = self.c.query()

    def test_source_removal_survives_sync_reopen_and_explicit_readd(self):
        label, digest = self.row['board_id'], self.row['digest']
        original = [p.read_bytes() for p in self.paths]
        with self.c.db:
            self.c.db.execute('UPDATE boards SET name=? WHERE id=?', ('unique-label', label))
        self.assertTrue(self.c.remove_from_label(label, digest))
        self.assertEqual(len(self.c.query()), 2)
        self.assertEqual(len(self.c.query_filtered([label])), 1)
        self.assertEqual(len(self.c.query([label])), 1)
        self.assertNotIn(digest, [r['digest'] for r in self.c.query(text='unique-label')])
        self.assertEqual(next(g['count'] for g in self.c.label_rows() if g['id'] == label), 1)
        self.c.sync_source(self.source)
        with Catalog(self.c.root) as reopened:
            self.assertEqual(reopened.image_labels(digest), [])
            self.assertEqual(len(reopened.query()), 2)
            self.assertEqual(reopened.add_to_label(label, [digest]), 1)
            self.assertEqual(len(reopened.collection_query(label)), 2)
        self.assertEqual([p.read_bytes() for p in self.paths], original)

    def test_manual_and_added_source_membership_only_detach_one_image(self):
        digest = self.row['digest']
        for row in [self.row, self.other]:
            self.c.set_tags(row['digest'], ['保留', '取消'])
        label = next(g['id'] for g in self.c.label_rows() if g['name'] == '取消')
        work = self.c.create_work_group('作品', [digest])
        self.assertTrue(self.c.remove_from_label(label, digest))
        self.assertEqual(self.c.tags_for(digest), ['保留'])
        self.assertIn('取消', self.c.tags_for(self.other['digest']))
        self.assertEqual(len(self.c.collection_query(f'work:{work}')), 1)
        self.assertFalse(self.c.remove_from_label(label, digest))
        imported = self.c.import_images(self.paths)['board_id']
        self.assertTrue(self.c.remove_from_label(imported, digest))
        self.assertEqual(len(self.c.query()), 2)
        self.assertEqual(len(self.c.collection_query(imported)), 1)
        self.assertEqual(self.c.add_to_label(imported, [digest]), 1)
        self.assertEqual(len(self.c.collection_query(imported)), 2)

    def test_local_addition_to_another_source_label_and_migration_backup(self):
        folder = self.source / 'history' / 'DEF456'
        folder.mkdir()
        Image.new('RGB', (80, 60), 'green').save(folder/'green.png')
        self.c.sync_source(self.source)
        label = next(g['id'] for g in self.c.label_rows() if g['id'] != self.row['board_id'])
        digest = self.row['digest']
        self.c.add_to_label(label, [digest])
        self.assertTrue(self.c.remove_from_label(label, digest))
        self.assertEqual(len(self.c.collection_query(label)), 1)
        self.assertIn(self.row['board_id'], [g['id'] for g in self.c.image_labels(digest)])
        with self.c.db:
            self.c.db.execute('DROP TABLE excluded_label_members')
            self.c.db.execute('PRAGMA user_version=5')
        with Catalog(self.c.root) as reopened:
            self.assertEqual(reopened.db.execute('PRAGMA user_version').fetchone()[0], 6)
            self.assertEqual(len(reopened.query()), 3)
        self.assertTrue(list((self.c.root/'backups').glob('before-v6-*.sqlite3')))


class ImageTagUiTests(unittest.TestCase):
    setUp = test_qt.QtFlowTests.setUp
    tearDown = test_qt.QtFlowTests.tearDown

    def test_chip_click_updates_other_window_and_manual_editor(self):
        c, w = self.controller.catalog, self.window
        row = w.rows[0]
        c.set_tags(row['digest'], ['可取消', '仍保留'])
        label = next(g['id'] for g in c.label_rows() if g['name'] == '可取消')
        second = self.controller.new_library()
        second.show_photo(row)
        w.show_photo(row)
        test_qt.drain(self.controller)
        QTest.mouseClick(w.tag_remove_buttons[label], Qt.LeftButton)
        test_qt.drain(self.controller)
        self.assertNotIn(label, w.tag_remove_buttons)
        self.assertNotIn(label, second.tag_remove_buttons)
        self.assertEqual(w.tag_editor.text(), '仍保留')
        self.assertEqual(second.tag_editor.text(), '仍保留')
        source = row['board_id']
        QTest.mouseClick(w.tag_remove_buttons[source], Qt.LeftButton)
        test_qt.drain(self.controller)
        self.assertNotIn(source, w.tag_remove_buttons)
        self.assertEqual(len(w.rows), 8)
        self.assertTrue(c.has_image(row['digest']))
        w.show_photo(row)  # stale history must not revive the removed chip
        self.assertNotIn(source, w.tag_remove_buttons)

    def test_removing_active_label_refreshes_filter_and_board_and_long_bar_scrolls(self):
        w, c = self.window, self.controller.catalog
        row = w.rows[0]
        label = row['board_id']
        c.set_tags(row['digest'], ['long reference label %02d' % i for i in range(12)])
        w.select_group(label)
        test_qt.drain(self.controller)
        before = len(w.rows)
        w.show_photo(row)
        board = self.controller.show_board(label)
        test_qt.drain(self.controller)
        self.assertGreater(w.source_tags.horizontalScrollBar().maximum(), 0)
        w.remove_image_label(row['digest'], label)
        test_qt.drain(self.controller)
        self.assertEqual(len(w.rows), before - 1)
        self.assertIsNone(w.current)
        self.assertNotIn(row['digest'], board.pictures)
        self.assertEqual(len(c.query()), 8)

    def test_thumbnail_submenu_targets_clicked_image_even_with_multiselection(self):
        w, c = self.window, self.controller.catalog
        first, other = w.rows[:2]
        source = first['board_id']
        for i in range(2):
            w.grid.selectionModel().select(w.model.index(i), QItemSelectionModel.Select)
        name = next(g['name'] for g in c.label_rows() if g['id'] == source)
        class ChooseMenu(QMenu):
            def exec(menu, *_):
                submenu = next(a.menu() for a in menu.actions() if a.text() == '取消标签')
                next(a for a in submenu.actions() if a.text() == name).trigger()
        pos = w.grid.visualRect(w.model.index(0)).center()
        with patch('snipboard.qt_library.QMenu', ChooseMenu):
            w.grid.customContextMenuRequested.emit(pos)
        test_qt.drain(self.controller)
        self.assertNotIn(source, [g['id'] for g in c.image_labels(first['digest'])])
        self.assertIn(source, [g['id'] for g in c.image_labels(other['digest'])])
        self.assertEqual(len(w.rows), 8)
        menu = QMenu(w)
        submenu = w.add_remove_label_menu(menu, first['digest'])
        self.assertEqual(submenu.actions()[0].text(), '暂无标签')
        self.assertFalse(submenu.actions()[0].isEnabled())
