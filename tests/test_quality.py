import json
from pathlib import Path
import sqlite3
import sys
import threading
import unittest
from unittest.mock import patch
import zipfile

from PySide6.QtCore import QEvent
from PIL import Image

import test_catalog
import test_qt
from snipboard.catalog import Catalog
from snipboard.maintenance import inspect_library, restore_backup
from snipboard.qt_maintenance import RecoveryDialog
from snipboard.runtime import configure_logging, close_logging


class MaintenanceTests(unittest.TestCase):
    setUp = test_catalog.CatalogTests.setUp

    def test_verified_backup_roundtrip_keeps_deleted_labels_groups_and_layout(self):
        self.catalog.sync_source(self.source)
        row = self.catalog.query()[0]
        self.catalog.set_tags(row['digest'], ['待恢复', '保留'])
        tag = next(g['id'] for g in self.catalog.label_rows() if g['name'] == '待恢复')
        group = self.catalog.create_work_group('作品', [row['digest']])
        layout = {'items': {row['digest']: dict(x=10, y=20, w=300, h=200, angle=35)}}
        self.catalog.save_layout(f'work:{group}', layout)
        self.catalog.remove_label(tag)
        self.catalog.delete_work_group(group)
        self.catalog.delete_images([row['digest']])
        archive = self.root / 'backup.zip'
        self.catalog.backup(archive)
        destination = self.root / 'restored'
        result = restore_backup(archive, destination, Catalog.schema_version)
        self.assertEqual(result['images'], 1)
        with Catalog(destination) as restored:
            self.assertEqual(restored.query(), [])
            self.assertEqual(restored.restore_images([row['digest']]), 1)
            self.assertEqual(restored.restore_labels([tag]), 1)
            self.assertEqual(restored.restore_work_groups([group]), 1)
            self.assertEqual(restored.tags_for(row['digest']), ['保留', '待恢复'])
            self.assertEqual(restored.layout(f'work:{group}'), layout)
            self.assertIsNone(restored.get_setting('work.current'))
            self.assertEqual(len(restored.collection_query(f'work:{group}')), 1)
        with self.assertRaises(FileExistsError):
            restore_backup(archive, destination, Catalog.schema_version)

    def test_failed_backup_never_publishes_partial_archive_or_overwrites(self):
        self.catalog.sync_source(self.source)
        archive = self.root / 'backup.zip'
        with patch('zipfile.ZipFile.write', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.catalog.backup(archive)
        self.assertFalse(archive.exists())
        self.assertEqual(list(self.root.glob('*.partial')), [])
        archive.write_bytes(b'keep existing')
        with self.assertRaises(FileExistsError):
            self.catalog.backup(archive)
        self.assertEqual(archive.read_bytes(), b'keep existing')

    def test_restore_rejects_unsafe_paths_and_corrupt_assets_without_publishing(self):
        self.catalog.sync_source(self.source)
        original = self.root / 'good.zip'
        self.catalog.backup(original)
        for name, bad_content in [('escape.zip', {'../outside.txt': b'bad'}), ('corrupt.zip', None)]:
            target = self.root / name
            with zipfile.ZipFile(original) as source, zipfile.ZipFile(target, 'w') as output:
                for entry in source.infolist():
                    content = source.read(entry)
                    if bad_content is None and entry.filename.startswith('assets/'):
                        content = b'corrupt'
                    output.writestr(entry.filename, content)
                if bad_content:
                    for path, content in bad_content.items():
                        output.writestr(path, content)
            destination = self.root / (name + '-restored')
            with self.assertRaises(ValueError):
                restore_backup(target, destination, Catalog.schema_version)
            self.assertFalse(destination.exists())
        self.assertFalse((self.root / 'outside.txt').exists())

    def test_integrity_detects_corrupt_copy_and_repairs_missing_thumbnail(self):
        self.catalog.sync_source(self.source)
        row = self.catalog.query()[0]
        thumbnail = Path(row['thumbnail'])
        thumbnail.unlink()
        report = inspect_library(self.catalog, rebuild=True)
        self.assertEqual((report['database'], report['missing'], report['corrupt']), ([], [], []))
        self.assertEqual(report['rebuilt'], 1)
        self.assertTrue(thumbnail.exists())
        asset = Path(row['asset'])
        asset.write_bytes(b'broken')
        self.assertEqual(len(inspect_library(self.catalog)['corrupt']), 1)
        asset.unlink()
        self.assertEqual(len(inspect_library(self.catalog)['missing']), 1)
        self.assertEqual(self.catalog.sync_source(self.source)['errors'], [])
        self.assertEqual(inspect_library(self.catalog)['missing'], [])
        self.assertEqual(asset.read_bytes(), self.image.read_bytes())

    def test_offline_source_preserves_inventory_and_invalid_layout_is_ignored(self):
        self.catalog.sync_source(self.source)
        group = self.catalog.group_rows()[0]['id']
        self.source.rename(self.root / 'offline')
        with self.assertRaises(ValueError):
            self.catalog.sync_source(self.source)
        self.assertEqual(len(self.catalog.query()), 1)
        with self.catalog.db:
            self.catalog.db.execute('INSERT INTO layouts VALUES(?,?)', (group, '{invalid'))
        self.assertIsNone(self.catalog.layout(group))
        self.catalog.save_layout(group, {'items': {'bad': {'w': float('nan')}}, 'viewport': ['bad']})
        self.assertEqual(self.catalog.layout(group), {'items': {}})
        self.catalog.set_setting('windows', 'invalid')
        self.assertEqual(self.catalog.get_setting('windows', []), [])

    def test_log_hook_does_not_accumulate_across_sessions(self):
        original = sys.excepthook
        for _ in range(3):
            configure_logging(self.root)
            self.assertIsNot(sys.excepthook, original)
            close_logging()
            self.assertIs(sys.excepthook, original)


class DesktopReliabilityTests(unittest.TestCase):
    setUp = test_qt.QtFlowTests.setUp
    tearDown = test_qt.QtFlowTests.tearDown

    def test_latest_background_request_supersedes_obsolete_queries(self):
        jobs = self.controller.jobs
        jobs.pool.setMaxThreadCount(1)
        started, release = threading.Event(), threading.Event()
        executed, delivered = [], []
        def work(index):
            executed.append(index)
            if index == 0:
                started.set()
                release.wait(3)
            return index
        jobs.submit(lambda: work(0), delivered.append, owner=self.window, key='latest-test')
        self.assertTrue(started.wait(2))
        for index in range(1, 30):
            jobs.submit(lambda i=index: work(i), delivered.append, owner=self.window, key='latest-test')
        release.set()
        test_qt.drain(self.controller)
        self.assertEqual(executed, [0, 29])
        self.assertEqual(delivered, [29])

    def test_closed_window_does_not_receive_failed_worker_callback(self):
        self.controller.new_library()
        release = threading.Event()
        delivered = []
        def work():
            release.wait(3)
            raise ValueError('simulated worker failure')
        self.controller.jobs.submit(work, failed=lambda error: delivered.append(error), owner=self.window)
        self.window.close()
        test_qt.APP.sendPostedEvents(None, QEvent.DeferredDelete)
        release.set()
        test_qt.drain(self.controller)
        self.assertEqual(delivered, [])

    def test_recovery_dialog_restores_selected_picture_and_invalid_geometry_falls_back(self):
        digest = self.window.rows[0]['digest']
        self.controller.delete_images([digest])
        test_qt.drain(self.controller)
        dialog = RecoveryDialog(self.controller, self.window)
        self.assertEqual(dialog.lists[0].count(), 1)
        dialog.lists[0].item(0).setSelected(True)
        dialog.restore_selected()
        test_qt.drain(self.controller)
        self.assertEqual(len(self.window.rows), 8)
        self.assertEqual(dialog.lists[0].count(), 0)
        dialog.close()
        self.controller.catalog.set_setting('board.geometry', ['invalid', None, -1, 0])
        board = self.controller.show_board()
        self.assertGreater(board.width(), 0)
