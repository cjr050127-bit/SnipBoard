"""Atomic local writes use synthetic fixtures under the project build directory."""
from __future__ import annotations

import errno
import hashlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from snipboard import atomic_io
from snipboard.catalog import Catalog
from snipboard.library import Library


TEST_TEMP = Path(__file__).resolve().parents[1] / 'build' / 'test-temp'
REAL_REPLACE = os.replace


def cross_device_error(source, destination, *, winerror=17, error_number=errno.EXDEV):
    error = OSError(error_number, 'simulated different disk drive', str(source))
    if winerror is not None:
        error.winerror = winerror
    error.filename2 = str(destination)
    return error


def picture_bytes(color='#c86d49'):
    output = io.BytesIO()
    Image.new('RGB', (79, 123), color).save(output, format='PNG')
    return output.getvalue()


class AtomicIoTests(unittest.TestCase):
    def setUp(self):
        TEST_TEMP.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix='atomic-io-', dir=TEST_TEMP)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / '.import-complete'
        self.target = self.root / 'stored.png'
        self.source.write_bytes(b'complete new content')

    def test_direct_replace_publishes_complete_file(self):
        with patch.object(atomic_io, '_physical_file_path') as physical:
            atomic_io.replace_local_file(self.source, self.target)
        physical.assert_not_called()
        self.assertEqual(self.target.read_bytes(), b'complete new content')
        self.assertFalse(self.source.exists())

    def test_direct_replace_overwrites_existing_target_atomically(self):
        self.target.write_bytes(b'old target')
        atomic_io.replace_local_file(self.source, self.target)
        self.assertEqual(self.target.read_bytes(), b'complete new content')
        self.assertFalse(self.source.exists())

    @unittest.skipUnless(os.name == 'nt', 'Windows AppX recovery')
    def test_winerror17_retries_with_physical_source_and_sibling_target(self):
        calls = []

        def replace(source, destination):
            calls.append((Path(source), Path(destination)))
            if len(calls) == 1:
                raise cross_device_error(source, destination, error_number=errno.EACCES)
            return REAL_REPLACE(source, destination)

        with patch.object(atomic_io.os, 'replace', side_effect=replace), \
                patch.object(atomic_io, '_physical_file_path', side_effect=lambda path: Path(path)) as physical:
            atomic_io.replace_local_file(self.source, self.target)

        self.assertEqual(calls, [(self.source, self.target), (self.source, self.target)])
        self.assertEqual(physical.call_args_list[0].args, (self.source,))
        self.assertEqual(self.target.read_bytes(), b'complete new content')
        self.assertFalse(self.source.exists())

    @unittest.skipUnless(os.name == 'nt', 'Windows AppX recovery')
    def test_errno_exdev_without_winerror_can_recover(self):
        calls = []

        def replace(source, destination):
            calls.append((source, destination))
            if len(calls) == 1:
                raise cross_device_error(source, destination, winerror=None)
            return REAL_REPLACE(source, destination)

        with patch.object(atomic_io.os, 'replace', side_effect=replace), \
                patch.object(atomic_io, '_physical_file_path', side_effect=lambda path: Path(path)):
            atomic_io.replace_local_file(self.source, self.target)
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.target.read_bytes(), b'complete new content')

    def test_permission_denied_does_not_query_physical_path_or_retry(self):
        self.target.write_bytes(b'old target')
        denied = PermissionError(errno.EACCES, 'simulated sharing violation')
        with patch.object(atomic_io.os, 'replace', side_effect=denied) as replace, \
                patch.object(atomic_io, '_physical_file_path') as physical:
            with self.assertRaises(PermissionError) as raised:
                atomic_io.replace_local_file(self.source, self.target)
        self.assertIs(raised.exception, denied)
        self.assertEqual(replace.call_count, 1)
        physical.assert_not_called()
        self.assertEqual(self.target.read_bytes(), b'old target')
        self.assertEqual(self.source.read_bytes(), b'complete new content')

    def test_different_logical_parent_does_not_use_sibling_mapping(self):
        other = self.root / 'other'
        other.mkdir()
        destination = other / self.target.name
        destination.write_bytes(b'old target')
        cross = cross_device_error(self.source, destination)
        with patch.object(atomic_io.os, 'replace', side_effect=cross) as replace, \
                patch.object(atomic_io, '_physical_file_path') as physical:
            with self.assertRaises(OSError) as raised:
                atomic_io.replace_local_file(self.source, destination)
        self.assertIs(raised.exception, cross)
        self.assertEqual(replace.call_count, 1)
        physical.assert_not_called()
        self.assertEqual(destination.read_bytes(), b'old target')
        self.assertTrue(self.source.exists())

    @unittest.skipUnless(os.name == 'nt', 'Windows AppX recovery')
    def test_physical_path_query_failure_preserves_existing_target(self):
        self.target.write_bytes(b'old target')
        cross = cross_device_error(self.source, self.target)
        with patch.object(atomic_io.os, 'replace', side_effect=cross) as replace, \
                patch.object(atomic_io, '_physical_file_path', side_effect=OSError('handle query failed')):
            with self.assertRaises(OSError):
                atomic_io.replace_local_file(self.source, self.target)
        self.assertEqual(replace.call_count, 1)
        self.assertEqual(self.target.read_bytes(), b'old target')
        self.assertEqual(self.source.read_bytes(), b'complete new content')

    @unittest.skipUnless(os.name == 'nt', 'Windows AppX recovery')
    def test_existing_target_at_other_physical_location_is_not_replaced(self):
        self.target.write_bytes(b'old target')
        redirected = self.root / 'physical-storage'
        redirected.mkdir()
        real_source = redirected / self.source.name
        real_source.write_bytes(b'complete new content')
        cross = cross_device_error(self.source, self.target)

        def physical(path):
            return real_source if Path(path) == self.source else self.target

        with patch.object(atomic_io.os, 'replace', side_effect=cross) as replace, \
                patch.object(atomic_io, '_physical_file_path', side_effect=physical):
            with self.assertRaises(OSError) as raised:
                atomic_io.replace_local_file(self.source, self.target)
        self.assertIs(raised.exception, cross)
        self.assertEqual(replace.call_count, 1)
        self.assertEqual(self.target.read_bytes(), b'old target')
        self.assertEqual(real_source.read_bytes(), b'complete new content')
        self.assertFalse((redirected / self.target.name).exists())

    @unittest.skipUnless(os.name == 'nt', 'Windows AppX recovery')
    def test_hidden_existing_physical_target_is_not_overwritten(self):
        redirected = self.root / 'physical-storage'
        redirected.mkdir()
        real_source = redirected / self.source.name
        real_target = redirected / self.target.name
        real_source.write_bytes(b'complete new content')
        real_target.write_bytes(b'hidden complete old target')
        cross = cross_device_error(self.source, self.target)
        with patch.object(atomic_io.os, 'replace', side_effect=cross) as replace, \
                patch.object(atomic_io, '_physical_file_path', return_value=real_source):
            with self.assertRaises(OSError) as raised:
                atomic_io.replace_local_file(self.source, self.target)
        self.assertIs(raised.exception, cross)
        self.assertEqual(replace.call_count, 1)
        self.assertEqual(real_target.read_bytes(), b'hidden complete old target')
        self.assertTrue(real_source.exists())
        self.assertFalse(self.target.exists())

    @unittest.skipUnless(os.name == 'nt', 'Windows AppX recovery')
    def test_existing_target_at_expected_physical_location_can_be_updated(self):
        self.target.write_bytes(b'old target')
        calls = []

        def replace(source, destination):
            calls.append((source, destination))
            if len(calls) == 1:
                raise cross_device_error(source, destination)
            return REAL_REPLACE(source, destination)

        with patch.object(atomic_io.os, 'replace', side_effect=replace), \
                patch.object(atomic_io, '_physical_file_path', side_effect=lambda path: Path(path)):
            atomic_io.replace_local_file(self.source, self.target)
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.target.read_bytes(), b'complete new content')
        self.assertFalse(self.source.exists())

    @unittest.skipUnless(os.name == 'nt', 'Windows AppX recovery')
    def test_visibility_check_failure_keeps_complete_published_file(self):
        calls = []

        def replace(source, destination):
            calls.append((source, destination))
            if len(calls) == 1:
                raise cross_device_error(source, destination)
            return REAL_REPLACE(source, destination)

        with patch.object(atomic_io.os, 'replace', side_effect=replace), \
                patch.object(atomic_io, '_physical_file_path', side_effect=lambda path: Path(path)), \
                patch.object(atomic_io.os.path, 'samefile', return_value=False):
            with self.assertRaises(OSError) as raised:
                atomic_io.replace_local_file(self.source, self.target)
        self.assertEqual(raised.exception.errno, errno.EXDEV)
        self.assertEqual(self.target.read_bytes(), b'complete new content')
        self.assertFalse(self.source.exists())

    @unittest.skipUnless(os.name == 'nt', 'Windows AppX recovery')
    def test_second_replace_failure_preserves_old_target_and_complete_source(self):
        self.target.write_bytes(b'old target')
        cross = cross_device_error(self.source, self.target)
        denied = PermissionError(errno.EACCES, 'simulated physical target locked')
        with patch.object(atomic_io.os, 'replace', side_effect=[cross, denied]) as replace, \
                patch.object(atomic_io, '_physical_file_path', side_effect=lambda path: Path(path)):
            with self.assertRaises(OSError):
                atomic_io.replace_local_file(self.source, self.target)
        self.assertEqual(replace.call_count, 2)
        self.assertEqual(self.target.read_bytes(), b'old target')
        self.assertEqual(self.source.read_bytes(), b'complete new content')

    @unittest.skipUnless(os.name == 'nt', 'GetFinalPathNameByHandleW')
    def test_real_windows_file_handle_returns_the_same_unicode_file(self):
        source = self.root / '含空格 参考图片.png'
        source.write_bytes(picture_bytes())
        physical = Path(atomic_io._physical_file_path(source))
        self.assertTrue(physical.is_absolute())
        self.assertTrue(os.path.samefile(source, physical))
        self.assertEqual(physical.read_bytes(), source.read_bytes())


class AtomicImportIntegrationTests(unittest.TestCase):
    def setUp(self):
        TEST_TEMP.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix='atomic-import-', dir=TEST_TEMP)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def catalog(self):
        catalog = Catalog(self.root / 'library')
        self.addCleanup(catalog.close)
        return catalog

    def test_asset_and_thumbnail_writers_use_shared_atomic_helper(self):
        catalog = self.catalog()
        with patch('snipboard.catalog.replace_local_file', wraps=atomic_io.replace_local_file) as replace:
            digest = catalog._asset(picture_bytes())
            catalog.ensure_feature(digest)
        self.assertEqual(replace.call_count, 2)
        targets = [Path(call.args[1]) for call in replace.call_args_list]
        self.assertEqual([target.parent.name for target in targets], ['assets', 'thumbnails'])
        self.assertEqual(hashlib.sha256(targets[0].read_bytes()).hexdigest(), digest)
        with Image.open(targets[1]) as image:
            image.load()

    @unittest.skipUnless(os.name == 'nt', 'Windows AppX recovery')
    def test_jpg_folder_import_recovers_asset_and_thumbnail_cross_device_errors(self):
        catalog = self.catalog()
        folder = self.root / '人物参考'
        folder.mkdir()
        source = folder / '示例.jpg'
        Image.new('RGB', (101, 157), '#bd6a45').save(source)
        original = source.read_bytes()
        faulted = set()

        def replace(source_path, destination):
            source_path = Path(source_path)
            kind = source_path.parent.name
            if kind not in faulted:
                faulted.add(kind)
                raise cross_device_error(source_path, destination)
            return REAL_REPLACE(source_path, destination)

        with patch.object(atomic_io.os, 'replace', side_effect=replace), \
                patch.object(atomic_io, '_physical_file_path', side_effect=lambda path: Path(path)):
            report = catalog.import_folder(folder)

        self.assertEqual((report['added'], report['errors']), (1, []))
        self.assertEqual(faulted, {'assets', 'thumbnails'})
        self.assertEqual(source.read_bytes(), original)
        rows = catalog.collection_query(report['label_id'])
        self.assertEqual(len(rows), 1)
        self.assertEqual(catalog.tags_for(rows[0]['digest']), ['人物参考'])
        with Image.open(rows[0]['asset']) as image:
            image.load()
            self.assertEqual(image.format, 'PNG')
        self.assertEqual(list((catalog.root / 'assets').glob('.import-*')), [])
        self.assertEqual(list(catalog.thumbnails.glob('.index-*')), [])

    def test_failed_thumbnail_does_not_publish_item_or_leave_temporary_files(self):
        catalog = self.catalog()
        folder = self.root / '未完成导入'
        folder.mkdir()
        source = folder / 'good.jpg'
        Image.new('RGB', (81, 129), '#bd6a45').save(source)
        original = source.read_bytes()

        def replace(source_path, destination):
            if Path(source_path).name.startswith('.index-'):
                raise PermissionError(errno.EACCES, 'simulated thumbnail locked')
            return REAL_REPLACE(source_path, destination)

        with patch.object(atomic_io.os, 'replace', side_effect=replace):
            report = catalog.import_folder(folder)

        self.assertEqual(report['added'], 0)
        self.assertEqual(len(report['errors']), 1)
        self.assertEqual(catalog.query(), [])
        self.assertIsNone(report['label_id'])
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(list((catalog.root / 'assets').glob('.import-*')), [])
        self.assertEqual(list(catalog.thumbnails.glob('.index-*')), [])

    def test_legacy_collection_uses_shared_atomic_helper(self):
        source = self.root / 'source'
        folder = source / 'history' / 'ABC123'
        folder.mkdir(parents=True)
        original = folder / 'synthetic.png'
        content = picture_bytes()
        original.write_bytes(content)
        with Library(self.root / 'library') as library:
            with patch('snipboard.library.replace_local_file', wraps=atomic_io.replace_local_file) as replace:
                report = library.collect(source, 'ABC123')
            self.assertEqual((report['added'], report['errors']), (1, []))
            self.assertEqual(replace.call_count, 1)
            row = library.search()[0]
            self.assertEqual(Path(row['asset']).read_bytes(), content)
        self.assertEqual(original.read_bytes(), content)


if __name__ == '__main__':
    unittest.main()
