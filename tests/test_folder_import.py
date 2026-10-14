"""Folder imports use temporary images and catalogs, never the user's library."""
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time
import traceback
import unittest
from unittest.mock import patch

from PIL import Image
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from snipboard.catalog import Catalog
from snipboard.qt_app import Controller
from test_qt import APP, drain


TEST_TEMP = Path(__file__).resolve().parents[1] / 'build' / 'test-temp'


def temporary_root():
    TEST_TEMP.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(prefix='folder-import-', dir=TEST_TEMP)


def picture(path, color, size=(91, 157)):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new('RGB', size, color).save(path)
    return path


class FolderImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = temporary_root()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.catalog = Catalog(self.root / 'library')
        self.addCleanup(self.catalog.close)

    def test_recursive_import_creates_folder_label_and_keeps_independent_copies(self):
        folder = self.root / '人物参考'
        paths = [picture(folder / 'red.PNG', '#df4338'),
                 picture(folder / '子目录' / 'blue.jpg', '#357bda'),
                 picture(folder / '子目录' / '再一层' / 'green.webp', '#36a354')]
        (folder / '说明.txt').write_text('This is not an image.', encoding='utf-8')
        before = {path: path.read_bytes() for path in paths}

        report = self.catalog.import_folder(folder)

        self.assertEqual((report['added'], report['duplicates'], report['errors']), (3, 0, []))
        self.assertFalse(report['cancelled'])
        self.assertRegex(report['label_id'], r'^tag:\d+$')
        label = next(row for row in self.catalog.label_rows() if row['id'] == report['label_id'])
        self.assertEqual((label['name'], label['kind'], label['count']), ('人物参考', 'manual', 3))
        rows = self.catalog.collection_query(report['label_id'])
        self.assertEqual({row['source_name'] for row in rows}, {path.name for path in paths})
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content)
            path.unlink()
        with Catalog(self.catalog.root) as reopened:
            retained = reopened.collection_query(report['label_id'])
            self.assertEqual(len(retained), 3)
            for row in retained:
                self.assertEqual(reopened.tags_for(row['digest']), ['人物参考'])
                with Image.open(row['asset']) as asset:
                    asset.load()
                    self.assertEqual(asset.format, 'PNG')

    def test_existing_source_duplicates_get_folder_tag_without_losing_other_memberships(self):
        source = self.root / 'source'
        original = picture(source / 'history' / 'ABC123' / 'red.png', '#d94537')
        self.catalog.sync_source(source)
        digest = self.catalog.query()[0]['digest']
        self.catalog.set_tags(digest, ['原有标签'])
        work = self.catalog.create_work_group('当前作品', [digest])
        self.catalog.set_current_work_group(work)
        folder = self.root / '人物参考'
        folder.mkdir()
        for name in ['copy-one.png', 'copy-two.png']:
            shutil.copyfile(original, folder / name)

        report = self.catalog.import_folder(folder)
        repeated = self.catalog.import_folder(folder)

        self.assertEqual((report['added'], report['duplicates'], report['errors']), (1, 1, []))
        self.assertEqual((repeated['added'], repeated['duplicates']), (0, 2))
        self.assertEqual(repeated['label_id'], report['label_id'])
        self.assertEqual(self.catalog.tags_for(digest), ['人物参考', '原有标签'])
        self.assertEqual(len(self.catalog.query()), 1)
        self.assertEqual(len(self.catalog.collection_query(report['label_id'])), 1)
        self.assertEqual(len(self.catalog.query()[0]['memberships']), 2)
        self.assertEqual(self.catalog.get_setting('work.current'), work)
        self.assertEqual([(row['id'], row['count']) for row in self.catalog.work_groups()], [(work, 1)])

    def test_equal_folder_names_reuse_one_tag_and_reimport_restores_removed_membership(self):
        # Leading spaces are valid on Windows; trailing spaces are normalized by
        # Win32 before Python can create image files inside the directory.
        first = self.root / '位置一' / '  色彩参考'
        second = self.root / '位置二' / '色彩参考'
        picture(first / 'red.png', '#e34030')
        picture(second / 'blue.png', '#287bdd')
        one = self.catalog.import_folder(first)
        two = self.catalog.import_folder(second)
        self.assertEqual(one['label_id'], two['label_id'])
        label = next(row for row in self.catalog.label_rows() if row['id'] == one['label_id'])
        self.assertEqual((label['name'], label['count']), ('色彩参考', 2))
        digest = next(row['digest'] for row in self.catalog.query() if row['source_name'] == 'red.png')
        self.assertTrue(self.catalog.remove_from_label(one['label_id'], digest))
        self.assertEqual(len(self.catalog.collection_query(one['label_id'])), 1)

        repeated = self.catalog.import_folder(first)

        self.assertEqual((repeated['added'], repeated['duplicates']), (0, 1))
        self.assertEqual(repeated['label_id'], one['label_id'])
        self.assertEqual(len(self.catalog.collection_query(one['label_id'])), 2)
        self.assertEqual(len([row for row in self.catalog.label_rows() if row['kind'] == 'manual']), 1)

    def test_empty_failed_and_immediately_cancelled_imports_do_not_create_empty_labels(self):
        empty = self.root / '空目录'
        empty.mkdir()
        broken = self.root / '损坏图片'
        broken.mkdir()
        (broken / 'bad.png').write_bytes(b'not an image')
        cancelled = self.root / '取消目录'
        picture(cancelled / 'valid.png', '#dd7744')

        empty_report = self.catalog.import_folder(empty)
        broken_report = self.catalog.import_folder(broken)
        cancelled_report = self.catalog.import_folder(cancelled, cancelled=lambda: True)

        for report in [empty_report, broken_report, cancelled_report]:
            self.assertEqual(report['added'], 0)
            self.assertEqual(report['duplicates'], 0)
            self.assertIsNone(report['label_id'])
            self.assertIsNone(report['board_id'])
        self.assertEqual(empty_report['errors'], [])
        self.assertEqual(len(broken_report['errors']), 1)
        self.assertTrue(cancelled_report['cancelled'])
        self.assertEqual(self.catalog.label_rows(), [])
        self.assertEqual(self.catalog.query(), [])

    def test_cancellation_between_images_keeps_completed_image_in_folder_label(self):
        folder = self.root / '可取消导入'
        for index, color in enumerate(['#db4530', '#327cda', '#39a75c']):
            picture(folder / f'{index}.png', color)
        cancelled = threading.Event()
        processed = []

        def progress(count, name):
            processed.append((count, name))
            cancelled.set()

        report = self.catalog.import_folder(folder, cancelled=cancelled.is_set, progress=progress)

        self.assertTrue(report['cancelled'])
        self.assertEqual((report['added'], report['duplicates'], report['errors']), (1, 0, []))
        self.assertEqual(len(processed), 1)
        self.assertEqual(len(self.catalog.collection_query(report['label_id'])), 1)
        self.assertEqual(len(list(folder.glob('*.png'))), 3)

    def test_parent_scan_skips_catalog_assets_and_importing_inside_catalog_is_rejected(self):
        original = picture(self.root / 'old.png', '#db4433')
        self.catalog.import_images([original])
        original.unlink()
        picture(self.root / 'chosen.png', '#397dde')

        report = self.catalog.import_folder(self.root)

        self.assertEqual((report['added'], report['duplicates'], report['errors']), (1, 0, []))
        self.assertEqual({row['source_name'] for row in self.catalog.collection_query(report['label_id'])},
                         {'chosen.png'})
        self.assertEqual(len(self.catalog.query()), 2)
        for folder in [self.catalog.root, self.catalog.root / 'assets', self.catalog.root / 'thumbnails']:
            with self.subTest(folder=folder), self.assertRaises(ValueError):
                self.catalog.import_folder(folder)


class FolderImportUiTests(unittest.TestCase):
    def setUp(self):
        self.callback_errors = []
        self.previous_hook = sys.excepthook
        sys.excepthook = lambda *args: self.callback_errors.append(
            ''.join(traceback.format_exception(*args)))
        self.temp = temporary_root()
        self.root = Path(self.temp.name)
        self.controller = Controller(APP, None, self.root / 'library', native=False)
        self.controller.timer.stop()
        path = picture(self.root / 'existing.png', '#d44035', (160, 90))
        self.controller.catalog.import_images([path])
        self.window = self.controller.new_library()
        self.releases = []
        drain(self.controller)

    def tearDown(self):
        try:
            for release in self.releases:
                release.set()
            self.controller.quit()
            drain(self.controller)
            self.controller._finish_shutdown()
            APP.processEvents()
            self.temp.cleanup()
        finally:
            sys.excepthook = self.previous_hook
        self.assertEqual(self.callback_errors, [], '\n'.join(self.callback_errors))

    def wait_until(self, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            APP.processEvents()
            if predicate():
                return
            QTest.qWait(10)
        self.fail('Qt/worker state did not become ready')

    def test_file_menu_import_selects_folder_label_and_refreshes_other_window(self):
        folder = self.root / '人物参考'
        picture(folder / 'blue.png', '#347bdc')
        picture(folder / '子文件夹' / 'green.png', '#35a95a')
        second = self.controller.new_library()
        self.window.filter_panel.restore(dict(orientation='landscape'))
        self.window.show_photo(self.window.rows[0])
        drain(self.controller)
        file_menu = self.window.menuBar().actions()[0].menu()
        action = next(item for item in file_menu.actions() if item.text() == '导入文件夹…')
        self.assertEqual(action.shortcut().toString(), 'Ctrl+Shift+O')

        with patch('snipboard.qt_library.QFileDialog') as dialog:
            dialog.getExistingDirectory.return_value = str(folder)
            action.trigger()
        drain(self.controller)

        label = next(row for row in self.controller.catalog.label_rows() if row['name'] == '人物参考')
        self.assertFalse(self.controller.import_running)
        self.assertEqual(self.window.selected_groups, [label['id']])
        self.assertEqual(self.window.stack.currentIndex(), 0)
        self.assertEqual({row['source_name'] for row in self.window.rows}, {'blue.png', 'green.png'})
        self.assertEqual(len(second.rows), 3)
        for window in [self.window, second]:
            self.assertIn(label['id'], [window.groups.item(index).data(Qt.UserRole)
                                       for index in range(window.groups.count())])
        with patch('snipboard.qt_library.QFileDialog') as dialog:
            dialog.getExistingDirectory.return_value = ''
            QTest.mouseClick(self.window.import_folder_button, Qt.LeftButton)
            dialog.getExistingDirectory.assert_called_once()
        self.assertFalse(self.controller.import_running)
        self.assertEqual(len(self.controller.catalog.query()), 3)

    def test_toolbar_import_runs_in_worker_and_prevents_concurrent_folder_selection(self):
        folder = self.root / '工具栏导入'
        picture(folder / 'blue.png', '#387bdd')
        started, release = threading.Event(), threading.Event()
        self.releases.append(release)
        threads = []
        original = Catalog.import_folder

        def blocked(catalog, *args, **kwargs):
            threads.append(threading.get_ident())
            started.set()
            if not release.wait(5):
                raise AssertionError('Test did not release the folder import worker')
            return original(catalog, *args, **kwargs)

        second = self.controller.new_library()
        with patch.object(Catalog, 'import_folder', blocked), \
                patch('snipboard.qt_library.QFileDialog') as dialog:
            dialog.getExistingDirectory.return_value = str(folder)
            QTest.mouseClick(self.window.import_folder_button, Qt.LeftButton)
            self.wait_until(started.is_set)
            self.assertTrue(self.controller.import_running)
            second.import_folder()
            dialog.getExistingDirectory.assert_called_once()
            release.set()
            drain(self.controller)

        self.assertEqual(len(threads), 1)
        self.assertNotEqual(threads[0], threading.get_ident())
        self.assertFalse(self.controller.import_running)
        self.assertEqual(len(self.window.rows), 1)
        self.assertEqual(self.window.rows[0]['source_name'], 'blue.png')
        self.assertEqual(len(second.rows), 2)


if __name__ == '__main__':
    unittest.main()
