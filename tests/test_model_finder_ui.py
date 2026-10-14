"""Model mode calls are explicit; all responses are mocked and catalogs temporary."""
import threading
import unittest
from unittest.mock import patch

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

import test_finder_ui as finder_support
from test_qt import APP, drain
from snipboard.model_search import ModelConfig, ModelSearchResult


class ModelFinderUiTests(unittest.TestCase):
    def setUp(self):
        self.fixture = finder_support.FinderUiTests()
        self.fixture.setUp()
        self.window = self.fixture.window
        self.controller = self.fixture.controller
        self.config = ModelConfig('https://vision.example/v1', 'drawing-vision', 'fake-session-key',
                                  candidate_limit=3, batch_size=2, timeout=30)

    def tearDown(self):
        self.fixture.tearDown()

    def result(self, name='red-side.png', *, analyzed=3, total=5):
        row = dict(self.fixture.fixture_rows[name])
        return ModelSearchResult([row], analyzed, total, min(3, total),
                                 reasons={row['digest']: '侧脸朝左，符合描述'},
                                 warnings=('仅分析本次候选缩略图，其余图片未判断。',))

    def configure(self):
        with patch('snipboard.qt_model_settings.configure_model', return_value=self.config):
            self.window.configure_finder_model()
        APP.processEvents()

    def test_default_configuration_and_typing_do_not_call_model(self):
        self.assertEqual(self.window.finder_panel.mode.currentIndex(), 0)
        with patch('snipboard.qt_library.search_with_model') as model_search:
            self.configure()
            self.assertEqual(self.window.finder_panel.mode.currentIndex(), 1)
            self.assertIn('drawing-vision', self.window.finder_panel.model_hint.text())
            self.assertIn('3 张', self.window.finder_panel.model_hint.text())
            self.assertIn('发送缩略图', self.window.finder_panel.model_hint.text())
            self.window.finder_panel.query.setPlainText('侧脸朝左')
            QTest.qWait(40)
            APP.processEvents()
            model_search.assert_not_called()
            self.window.finder_panel.mode.setCurrentIndex(0)
            self.fixture.submit('人物')
            model_search.assert_not_called()
        self.assertEqual(len(self.fixture.names()), 4)
        self.assertFalse(self.window.finder_applied_model)

    def test_unconfigured_model_mode_does_not_send_anything(self):
        original = self.fixture.names()
        self.window.finder_panel.mode.setCurrentIndex(1)
        with patch('snipboard.qt_library.search_with_model') as model_search:
            self.fixture.submit('侧脸朝左')
            model_search.assert_not_called()
        self.assertEqual(self.fixture.names(), original)
        self.assertIn('接入模型', self.window.finder_panel.status.text())
        self.assertIsNone(self.window.finder_cancel)

    def test_model_mode_local_conditions_return_all_without_upload(self):
        self.configure()
        expected = self.fixture.names()
        with patch('snipboard.model_search._post_json', side_effect=AssertionError('Unexpected model request')):
            self.fixture.submit('全部图片')
        self.assertEqual(self.fixture.names(), expected)
        self.assertFalse(self.window.finder_applied_model)
        self.assertIn('本地条件已满足', self.window.heading.text())
        self.assertIn('未发送缩略图或调用模型', self.window.finder_panel.status.text())

    def test_explicit_model_search_shows_coverage_and_reason_tooltip(self):
        self.configure()
        with patch('snipboard.qt_library.search_with_model', return_value=self.result()) as model_search:
            self.fixture.submit('侧脸朝左')
            model_search.assert_called_once()
            args, kwargs = model_search.call_args
            self.assertEqual(args[2], '侧脸朝左')
            self.assertIs(args[3], self.config)
            self.assertIsNone(kwargs['scope_digests'])
        self.assertEqual(self.fixture.names(), {'red-side.png'})
        self.assertTrue(self.window.finder_applied_model)
        self.assertIn('已分析 3 / 5 张候选', self.window.summary.text())
        self.assertIn('其余图片未判断', self.window.finder_panel.status.text())
        self.assertIn('模型判断', self.window.heading.text())
        tooltip = self.window.model.index(0).data(Qt.ToolTipRole)
        self.assertIn('模型判断：侧脸朝左，符合描述', tooltip)

    def test_model_result_source_change_requires_new_explicit_search(self):
        self.configure()
        with patch('snipboard.qt_library.search_with_model', return_value=self.result()) as model_search:
            self.fixture.submit('侧脸朝左')
            previous = self.fixture.names()
            self.window.source_changed()
            drain(self.controller)
            model_search.assert_called_once()
        self.assertEqual(self.fixture.names(), previous)
        self.assertIn('再次点击找图', self.window.finder_panel.status.text())

    def test_removing_model_condition_waits_for_click_and_preserves_edited_plan(self):
        self.configure()
        self.window.finder_panel.scope.setCurrentIndex(1)
        original_scope = {r['digest'] for r in self.window.rows}
        text = '人物，侧脸朝左'
        with patch('snipboard.qt_library.search_with_model', return_value=self.result(total=4)) as model_search:
            self.fixture.submit(text)
            condition = next(c for c in self.window.finder_plan.conditions if c.kind == 'keyword')
            QTest.mouseClick(self.window.finder_panel.condition_buttons[condition.id], Qt.LeftButton)
            drain(self.controller)
            model_search.assert_called_once()
            self.assertIn('条件已修改', self.window.finder_panel.status.text())
            self.assertNotIn(condition.id, self.window.finder_panel.condition_buttons)
            self.fixture.submit(text)
            self.assertEqual(model_search.call_count, 2)
            args, kwargs = model_search.call_args
            self.assertNotIn(condition.id, {c.id for c in args[1].conditions})
            self.assertEqual({c.kind for c in args[1].conditions}, {'label'})
            self.assertEqual(set(kwargs['scope_digests']), original_scope)

    def test_local_result_source_change_never_uses_newly_selected_model_mode(self):
        self.fixture.submit('人物横图，不要教程')
        previous = self.fixture.names()
        self.configure()
        self.assertEqual(self.window.finder_panel.mode.currentIndex(), 1)
        with patch('snipboard.qt_library.search_with_model') as model_search:
            self.window.source_changed()
            drain(self.controller)
            model_search.assert_not_called()
        self.assertEqual(self.fixture.names(), previous)
        self.assertFalse(self.window.finder_applied_model)

    def test_cancelled_model_reply_cannot_replace_new_local_search(self):
        self.configure()
        started, release = threading.Event(), threading.Event()
        self.fixture.releases.append(release)
        cancellations = []

        def delayed(catalog, plan, text, config, cancel=None, **kwargs):
            cancellations.append(cancel)
            started.set()
            if not release.wait(5):
                raise AssertionError('Test did not release mocked model request')
            # Even a service reply that arrives after cancellation is stale.
            return self.result()

        with patch('snipboard.qt_library.search_with_model', side_effect=delayed) as model_search:
            self.fixture.submit('侧脸朝左', wait=False)
            self.fixture.wait_until(started.is_set)
            self.window.finder_panel.mode.setCurrentIndex(0)
            self.assertTrue(cancellations[0]())
            self.fixture.submit('风景', wait=False)
            release.set()
            drain(self.controller)
            model_search.assert_called_once()
        self.assertEqual(self.fixture.names(), {'green-landscape.png'})
        self.assertFalse(self.window.finder_applied_model)
        self.assertEqual(self.window.finder_applied_plan.query, '风景')
        self.assertIn('本地匹配', self.window.heading.text())


if __name__ == '__main__':
    unittest.main()
