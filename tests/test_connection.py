import os
from pathlib import Path
import tempfile
import threading
import subprocess
from unittest import TestCase
from unittest.mock import patch

from PIL import Image
from snipboard.catalog import Catalog
from snipboard.source import history_directory, normalize_source, inspect_source
from snipboard.discovery import discover_candidates, shortcut_locations
import test_qt


class DiscoveryTests(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = self.root / '便携软件 Snipaste'
        group = self.app / 'history' / 'ABC123'
        group.mkdir(parents=True)
        Image.new('RGB', (37, 53), 'blue').save(group / 'paste.png')
        (self.app / 'Snipaste.exe').touch()

    def providers(self, **paths):
        from contextlib import ExitStack
        stack = ExitStack()
        for name in ('running_executables', 'store_locations', 'registry_locations', 'shortcut_locations', 'common_locations'):
            stack.enter_context(patch('snipboard.discovery.' + name, return_value=paths.get(name, [])))
        return stack

    def test_portable_process_store_and_shortcut_candidates_deduplicate_by_history(self):
        other = self.root / 'store' / 'history'
        other.mkdir(parents=True)
        with self.providers(running_executables=[self.app / 'Snipaste.exe'],
                            shortcut_locations=[self.app / 'Snipaste.exe'], store_locations=[other.parent]):
            result = discover_candidates()
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]['groups'], 1)
        self.assertEqual(result[0]['reason'], '正在运行')
        self.assertEqual(normalize_source(self.app / 'history'), self.app)

    def test_custom_history_relative_environment_and_fallback_sync_only_pasted(self):
        custom = self.root / 'custom storage'
        group = custom / 'DEF456'
        group.mkdir(parents=True)
        Image.new('RGB', (90, 121), 'red').save(group / 'paste.png')
        (custom / 'snip').mkdir()
        Image.new('RGB', (80, 80), 'green').save(custom / 'snip' / 'screenshot.png')
        config = self.app / 'config.ini'
        for value in ('../custom storage', '%SNIPBOARD_TEST_STORAGE%', str(custom).replace('\\', '\\\\')):
            config.write_text('[General]\nhistory_dir=' + value + '\n', encoding='utf-8-sig')
            with patch.dict(os.environ, {'SNIPBOARD_TEST_STORAGE': str(custom)}):
                self.assertEqual(history_directory(self.app), custom)
                self.assertEqual(normalize_source(config), self.app)
                before = {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
                with Catalog(self.root / ('library-' + str(len(value)))) as catalog:
                    report = catalog.sync_source(self.app)
                    self.assertEqual(report['files'], 1)
                    self.assertEqual(catalog.query()[0]['width'], 90)
                self.assertTrue(all(Path(p).read_bytes() == data for p, data in before.items()))
        config.write_text('[General]\nhistory_dir=missing-folder\n', encoding='utf-8')
        self.assertEqual(history_directory(self.app), self.app / 'history')

    def test_invalid_and_inaccessible_candidates_do_not_hide_valid_source(self):
        with self.providers(running_executables=[self.root / 'missing.exe'], common_locations=[self.app]):
            with patch('snipboard.discovery.registry_locations', side_effect=PermissionError('denied')):
                self.assertEqual(len(discover_candidates()), 1)
        with self.assertRaises(ValueError):
            normalize_source(self.root / 'missing')
        (self.app / 'config.ini').write_text('malformed config', encoding='utf-8')
        self.assertEqual(inspect_source(self.app)['groups'][0]['id'], 'ABC123')

    def test_windows_shortcut_resolves_without_executing_target(self):
        if os.name != 'nt':
            self.skipTest('Windows shell shortcuts')
        import json
        desktop = self.root / 'Desktop'
        desktop.mkdir()
        link = desktop / 'Snipaste 便携版.lnk'
        script = "$ErrorActionPreference='Stop'; $p=ConvertFrom-Json ([Console]::In.ReadToEnd()); $w=New-Object -ComObject WScript.Shell; $s=$w.CreateShortcut($p.link); $s.TargetPath=$p.target; $s.Save()"
        subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
            input=json.dumps(dict(link=str(link), target=str(self.app / 'Snipaste.exe'))),
            text=True, capture_output=True, check=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
        environment = {key: str(self.root / key) for key in ('PUBLIC', 'APPDATA', 'PROGRAMDATA', 'OneDrive')}
        with patch('snipboard.discovery.Path.home', return_value=self.root), patch.dict(os.environ, environment):
            self.assertEqual(shortcut_locations(), [self.app / 'Snipaste.exe'])


class ConnectionUiTests(TestCase):
    setUp = test_qt.QtFlowTests.setUp
    tearDown = test_qt.QtFlowTests.tearDown

    def candidate(self, path=None):
        path = path or self.source
        return dict(path=str(path), history=str(path / 'history'), reason='便携版', groups=2)

    def first_run(self):
        self.controller.source = None
        self.controller.catalog.set_setting('source', None)
        self.controller.start_connection()

    def test_unique_first_run_auto_connects_and_remembers_source(self):
        with patch('snipboard.qt_connection.discover_candidates', return_value=[self.candidate()]):
            self.first_run()
            test_qt.drain(self.controller)
        self.assertEqual(self.controller.source, self.source)
        self.assertTrue(self.controller.catalog.get_setting('connection.setup_done'))
        self.assertIsNone(self.controller.connection_dialog)
        self.assertEqual(len(self.window.rows), 8)

    def test_multiple_requires_explicit_selection_and_preserves_existing_source_on_cancel(self):
        other = self.root / 'other'
        (other / 'history').mkdir(parents=True)
        with patch('snipboard.qt_connection.discover_candidates', return_value=[self.candidate(), self.candidate(other)]):
            self.first_run()
            test_qt.drain(self.controller)
            dialog = self.controller.connection_dialog
            self.assertIsNone(self.controller.source)
            self.assertFalse(dialog.connect_button.isEnabled())
            dialog.locations.setCurrentRow(0)
            dialog.connect_button.click()
            test_qt.drain(self.controller)
        self.assertEqual(self.controller.source, self.source)
        with patch('snipboard.qt_connection.discover_candidates', return_value=[self.candidate(other)]):
            self.controller.choose_source()
            test_qt.drain(self.controller)
            self.assertEqual(self.controller.source, self.source)
            self.controller.connection_dialog.reject()
        self.assertEqual(self.controller.catalog.get_setting('source'), str(self.source))

    def test_empty_skip_does_not_reopen_on_next_start_and_can_reopen_from_menu(self):
        with patch('snipboard.qt_connection.discover_candidates', return_value=[]):
            self.first_run()
            test_qt.drain(self.controller)
            dialog = self.controller.connection_dialog
            self.assertIn('尚未找到', dialog.message.text())
            dialog.reject()
            self.controller.start_connection()
            self.assertIsNone(self.controller.connection_dialog)
            self.controller.choose_source()
            test_qt.drain(self.controller)
            self.assertIsNotNone(self.controller.connection_dialog)

    def test_manual_executable_connection_and_invalid_location(self):
        executable = self.source / 'Snipaste.exe'
        executable.touch()
        with patch('snipboard.qt_connection.discover_candidates', return_value=[]):
            self.first_run()
            test_qt.drain(self.controller)
            dialog = self.controller.connection_dialog
            dialog.connect_path(self.root / 'does-not-exist')
            self.assertIsNone(self.controller.source)
            self.assertIn('无法连接', dialog.message.text())
            with patch('snipboard.qt_connection.QFileDialog.getOpenFileName', return_value=(str(executable), '')):
                dialog.browse_file()
            test_qt.drain(self.controller)
        self.assertEqual(self.controller.source, self.source)

    def test_offline_saved_source_is_kept_and_not_automatically_replaced(self):
        missing = self.root / 'offline'
        self.controller.source = missing
        with patch('snipboard.qt_connection.discover_candidates', return_value=[self.candidate()]):
            self.controller.start_connection()
            test_qt.drain(self.controller)
        self.assertEqual(self.controller.source, missing)
        self.assertIsNotNone(self.controller.connection_dialog)
        self.assertEqual(len(self.window.rows), 8)

    def test_skip_while_scan_pending_never_connects_late(self):
        started, release = threading.Event(), threading.Event()
        def discovery():
            started.set()
            release.wait(3)
            return [self.candidate()]
        with patch('snipboard.qt_connection.discover_candidates', side_effect=discovery):
            self.first_run()
            test_qt.APP.processEvents()
            self.assertTrue(started.wait(1))
            self.controller.connection_dialog.reject()
            release.set()
            test_qt.drain(self.controller)
        self.assertIsNone(self.controller.source)
