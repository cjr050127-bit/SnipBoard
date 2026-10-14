"""Find pictures through the actual Qt panel, using only a temporary catalog."""
from pathlib import Path
import sys
import tempfile
import threading
import time
import traceback
import unittest
from unittest.mock import patch

from PIL import Image
from PySide6.QtCore import Qt, QThread
from PySide6.QtTest import QTest

from test_qt import APP, drain
from snipboard.qt_app import Controller
from snipboard.natural_search import search_catalog


class FinderUiTests(unittest.TestCase):
    def setUp(self):
        self.callback_errors = []
        self.previous_hook = sys.excepthook
        sys.excepthook = lambda *args: self.callback_errors.append(
            ''.join(traceback.format_exception(*args)))
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.controller = Controller(APP, None, self.root / 'library', native=False)
        self.controller.timer.stop()
        examples = [
            ('red-side.png', (160, 90), '#e34242', ['人物', '插画', '侧脸'], '逆光'),
            ('blue-photo.png', (90, 160), '#4385db', ['人物', '照片'], '自然光'),
            ('red-tutorial.png', (160, 90), '#ee5555', ['人物', '插画', '教程'], '教学拼图'),
            ('black-side.png', (160, 90), '#151515', ['人物', '插画', '侧脸'], '暗处'),
            ('green-landscape.png', (160, 90), '#43ad65', ['风景'], '草地'),
        ]
        for name, size, color, tags, note in examples:
            path = self.root / name
            Image.new('RGB', size, color).save(path)
            self.controller.catalog.import_images([path])
            row = next(r for r in self.controller.catalog.query() if r['source_name'] == name)
            self.controller.catalog.annotate(row['id'], note, tags)
        self.fixture_rows = {r['source_name']: r for r in self.controller.catalog.query()}
        self.window = self.controller.new_library()
        drain(self.controller)
        self.window.open_finder()
        APP.processEvents()
        self.releases = []

    def tearDown(self):
        for release in self.releases:
            release.set()
        self.controller.quit()
        drain(self.controller)
        self.controller._finish_shutdown()
        APP.processEvents()
        self.temp.cleanup()
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

    def submit(self, text, *, continued=False, wait=True, window=None):
        window = window or self.window
        panel = window.finder_panel
        panel.query.setPlainText(text)
        panel.continue_query.setChecked(continued)
        QTest.mouseClick(panel.search_button, Qt.LeftButton)
        if wait:
            drain(self.controller)

    def names(self, window=None):
        window = window or self.window
        return {r['source_name'] for r in window.model.rows}

    def blocking_search(self):
        started, release = threading.Event(), threading.Event()
        self.releases.append(release)

        def blocked(catalog, plan, cancel=None, **kwargs):
            started.set()
            if not release.wait(5):
                raise AssertionError('Test did not release the search worker')
            return search_catalog(catalog, plan, cancel, **kwargs)

        return started, release, blocked

    def test_chinese_request_returns_results_in_existing_grid(self):
        self.window.show_photo(self.fixture_rows['blue-photo.png'])
        drain(self.controller)
        self.assertEqual(self.window.stack.currentIndex(), 1)
        self.submit('人物标签里的横图，宽度至少160，不要教程')
        self.assertEqual(self.names(), {'red-side.png', 'black-side.png'})
        self.assertEqual(self.window.stack.currentIndex(), 0)
        self.assertEqual(len(self.controller.windows), 1)
        self.assertIn('2 张', self.window.summary.text())
        self.assertTrue(self.window.finder_panel.continue_query.isEnabled())
        self.assertFalse(self.window.finder_panel.cancel_button.isEnabled())

    def test_typing_and_opening_panel_do_not_start_search_workers(self):
        original = self.names()
        self.assertIsNone(self.controller.jobs.serial_pool)
        self.window.finder_panel.query.setPlainText('风景')
        QTest.qWait(40)
        APP.processEvents()
        self.assertEqual(self.names(), original)
        self.assertIsNone(self.controller.jobs.serial_pool)
        self.assertIsNone(self.window.finder_cancel)
        QTest.mouseClick(self.window.finder_panel.search_button, Qt.LeftButton)
        drain(self.controller)
        self.assertEqual(self.names(), {'green-landscape.png'})

    def test_follow_up_and_remove_condition_update_real_results(self):
        self.submit('人物')
        self.assertEqual(len(self.names()), 4)
        self.submit('只留横图，不要教程', continued=True)
        self.assertEqual(self.names(), {'red-side.png', 'black-side.png'})
        excluded = next(c for c in self.window.finder_plan.conditions if c.exclude)
        QTest.mouseClick(self.window.finder_panel.condition_buttons[excluded.id], Qt.LeftButton)
        drain(self.controller)
        self.assertEqual(self.names(), {'red-side.png', 'red-tutorial.png', 'black-side.png'})
        self.assertNotIn(excluded.id, self.window.finder_panel.condition_buttons)

    def test_return_to_original_filter_restores_regular_search(self):
        self.window.search.setText('tutorial')
        self.window.refresh()
        drain(self.controller)
        original = self.names()
        self.assertEqual(original, {'red-tutorial.png'})
        self.submit('人物')
        self.assertEqual(len(self.names()), 4)
        QTest.mouseClick(self.window.finder_panel.clear_button, Qt.LeftButton)
        drain(self.controller)
        self.assertEqual(self.names(), original)
        self.assertEqual(self.window.search.text(), 'tutorial')
        self.assertFalse(self.window.finder_active)
        self.assertFalse(self.window.finder_panel.continue_query.isChecked())
        self.assertFalse(self.window.finder_panel.continue_query.isEnabled())

    def test_empty_current_filter_scope_does_not_search_entire_library(self):
        self.window.search.setText('不存在的文件名')
        self.window.refresh()
        drain(self.controller)
        self.assertEqual(self.names(), set())
        self.window.finder_panel.scope.setCurrentIndex(1)
        self.submit('全部图片')
        self.assertEqual(self.names(), set())
        self.assertIn('找到 0 张', self.window.finder_panel.status.text())

    def test_current_work_group_requires_selection_and_limits_results(self):
        self.window.finder_panel.scope.setCurrentIndex(2)
        original = self.names()
        self.submit('全部图片')
        self.assertEqual(self.names(), original)
        self.assertIn('请先', self.window.finder_panel.status.text())
        group_names = {'red-side.png', 'blue-photo.png'}
        ident = self.controller.catalog.create_work_group(
            '人物参考', [self.fixture_rows[n]['digest'] for n in group_names])
        self.controller.catalog.set_current_work_group(ident)
        self.submit('全部图片')
        self.assertEqual(self.names(), group_names)
        self.submit('只留横图', continued=True)
        self.assertEqual(self.names(), {'red-side.png'})

    def test_removing_pending_condition_preserves_current_filter_scope(self):
        self.window.search.setText('side')
        self.window.refresh()
        drain(self.controller)
        original = self.names()
        self.assertEqual(original, {'red-side.png', 'black-side.png'})
        self.window.finder_panel.scope.setCurrentIndex(1)
        started, release, blocked = self.blocking_search()
        with patch('snipboard.qt_library.search_catalog', side_effect=blocked):
            self.submit('人物横图', wait=False)
            self.wait_until(started.is_set)
            orientation = next(c for c in self.window.finder_plan.conditions if c.kind == 'orientation')
            QTest.mouseClick(self.window.finder_panel.condition_buttons[orientation.id], Qt.LeftButton)
            release.set()
            drain(self.controller)
        self.assertEqual(self.names(), original)

    def test_explicit_scope_change_preserves_follow_up_conditions(self):
        self.window.search.setText('side')
        self.window.refresh()
        drain(self.controller)
        self.window.finder_panel.scope.setCurrentIndex(1)
        self.submit('人物')
        self.assertEqual(self.names(), {'red-side.png', 'black-side.png'})
        # Choosing the whole library changes the scope, while continuing keeps
        # the previous person condition and adds the requested exclusion.
        self.window.finder_panel.scope.setCurrentIndex(0)
        self.submit('不要教程', continued=True)
        self.assertEqual(self.names(), {'red-side.png', 'black-side.png', 'blue-photo.png'})
        self.assertEqual(self.window.finder_applied_plan.history, ('人物', '不要教程'))

    def test_hiding_panel_cancels_pending_search_and_preserves_last_results(self):
        self.submit('风景')
        previous = self.names()
        started, release, blocked = self.blocking_search()
        with patch('snipboard.qt_library.search_catalog', side_effect=blocked):
            self.submit('人物', wait=False)
            self.wait_until(started.is_set)
            cancellation = self.window.finder_cancel
            self.window.docks['finder'].hide()
            APP.processEvents()
            self.assertTrue(cancellation.is_set())
            self.assertIsNone(self.window.finder_cancel)
            self.assertFalse(self.window.finder_panel.cancel_button.isEnabled())
            release.set()
            drain(self.controller)
        self.assertEqual(self.names(), previous)
        self.assertIn('停止', self.window.finder_panel.status.text())
        self.assertEqual(self.window.finder_applied_plan.query, '风景')

    def test_closing_window_cancels_search_without_affecting_other_window(self):
        other = self.controller.new_library()
        drain(self.controller)
        started, release, blocked = self.blocking_search()
        with patch('snipboard.qt_library.search_catalog', side_effect=blocked):
            self.submit('人物', wait=False)
            self.wait_until(started.is_set)
            cancellation = self.window.finder_cancel
            self.window.close()
            self.assertTrue(cancellation.is_set())
            release.set()
            drain(self.controller)
        self.assertEqual(self.controller.windows, [other])
        self.assertEqual(len(self.names(other)), 5)
        self.assertFalse(self.controller.quitting)

    def test_new_request_wins_when_previous_worker_completes_late(self):
        started, release = threading.Event(), threading.Event()
        self.releases.append(release)

        def delayed(catalog, plan, cancel=None, **kwargs):
            if plan.query == '人物':
                started.set()
                if not release.wait(5):
                    raise AssertionError('Test did not release old request')
                # A worker that missed cancellation must still not replace the newer UI request.
                return search_catalog(catalog, plan, **kwargs)
            return search_catalog(catalog, plan, cancel, **kwargs)

        with patch('snipboard.qt_library.search_catalog', side_effect=delayed):
            self.submit('人物', wait=False)
            self.wait_until(started.is_set)
            self.submit('风景', wait=False)
            release.set()
            drain(self.controller)
        self.assertEqual(self.names(), {'green-landscape.png'})
        self.assertEqual(self.window.finder_applied_plan.query, '风景')
        self.assertIn('找到 1 张', self.window.finder_panel.status.text())

    def test_searches_share_one_low_priority_worker_and_do_not_analyze_images(self):
        other = self.controller.new_library()
        drain(self.controller)
        other.open_finder()
        started, release = threading.Event(), threading.Event()
        second_started = threading.Event()
        self.releases.append(release)
        running, maximum = 0, 0
        lock = threading.Lock()

        def observe(catalog, plan, cancel=None, **kwargs):
            nonlocal running, maximum
            with lock:
                running += 1
                maximum = max(maximum, running)
            try:
                if plan.query == '人物':
                    started.set()
                    if not release.wait(5):
                        raise AssertionError('Test did not release shared worker')
                else:
                    second_started.set()
                return search_catalog(catalog, plan, cancel, **kwargs)
            finally:
                with lock:
                    running -= 1

        with patch('snipboard.qt_library.search_catalog', side_effect=observe), \
                patch('snipboard.features.extract', side_effect=AssertionError('Unexpected image analysis')), \
                patch('PIL.Image.open', side_effect=AssertionError('Unexpected original image decoding')):
            self.submit('人物', wait=False)
            self.wait_until(started.is_set)
            self.submit('风景', wait=False, window=other)
            QTest.qWait(30)
            APP.processEvents()
            self.assertFalse(second_started.is_set())
            pool = self.controller.jobs.serial_pool
            self.assertEqual(pool.maxThreadCount(), 1)
            self.assertEqual(pool.threadPriority(), QThread.LowPriority)
            release.set()
            drain(self.controller)
        self.assertEqual(maximum, 1)
        self.assertEqual(len(self.names()), 4)
        self.assertEqual(self.names(other), {'green-landscape.png'})


if __name__ == '__main__':
    unittest.main()
