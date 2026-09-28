import hashlib
import json
from pathlib import Path
import struct
import tempfile
import unittest
import zlib

from snipboard.library import Library, validate_png
from snipboard.source import inspect_source, qstring, restore_observations


def qs(text):
    if text is None:
        return b'\xff' * 4
    encoded = text.encode('utf-16-be')
    return struct.pack('>I', len(encoded)) + encoded


def png():
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(b'\x00\xff\x00\x00')) + chunk(b'IEND', b''))


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / 'source'
        self.group = self.state / 'history' / 'ABC123'
        self.group.mkdir(parents=True)
        self.asset = self.group / 'image.png'
        self.asset.write_bytes(png())
        self.restore = qs('SNIPASTE-RESOTRE') + struct.pack('>I', 201) + b'\x01' + qs('ABC123') + qs('量子力学') + qs('DEF456')
        (self.state / 'history' / '.sp0').write_bytes(self.restore)
        (self.state / 'splog.txt').write_text('[2026-09-22 09:59:26.236] [I] Group 4 [ABC123]: "量子力学"\n', encoding='utf-8')

    def library(self):
        library = Library(self.root / 'library')
        self.addCleanup(library.close)
        return library

    def test_restore_evidence_never_becomes_live_state(self):
        snapshot = inspect_source(self.state)
        self.assertEqual(snapshot['groups'][0]['name_candidate'], '量子力学')
        self.assertEqual(snapshot['restore']['trailing_group_id_observation'], 'DEF456')
        self.assertEqual(snapshot['last_log_group_observation']['id'], 'ABC123')
        self.assertIsNone(snapshot['current_group']['id'])
        self.assertEqual(snapshot['current_group']['status'], 'unverified')

    def test_invalid_metadata_degrades_to_inventory(self):
        (self.state / 'history' / '.sp0').write_bytes(b'broken')
        result = inspect_source(self.state)
        self.assertTrue(result['warnings'])
        self.assertEqual(result['groups'][0]['image_files'], ('image.png',))

    def test_unknown_version_is_rejected(self):
        with self.assertRaises(ValueError):
            restore_observations(qs('SNIPASTE-RESOTRE') + struct.pack('>I', 202))

    def test_string_bounds(self):
        for data in (b'', b'\x00\x00\x00\x03abc', b'\x00\x00\x00\x10abc'):
            with self.assertRaises(ValueError):
                qstring(data, 0)

    def test_idempotent_and_source_deletion_retains_asset(self):
        library = self.library()
        self.assertEqual(library.collect(self.state, 'ABC123')['added'], 1)
        self.assertEqual(library.collect(self.state, 'ABC123')['added'], 0)
        self.asset.unlink()
        self.assertEqual(library.collect(self.state, 'ABC123')['added'], 0)
        rows = library.search()
        self.assertEqual(len(rows), 1)
        self.assertEqual(Path(rows[0]['asset']).read_bytes(), png())

    def test_independent_board_membership_shared_asset(self):
        second = self.state / 'history' / 'DEF456'
        second.mkdir()
        (second / 'second.png').write_bytes(png())
        library = self.library()
        library.collect(self.state, 'ABC123')
        library.collect(self.state, 'DEF456')
        self.assertEqual(len(library.search()), 2)
        self.assertEqual(len(list((library.root / 'assets').glob('*.png'))), 1)

    def test_partial_image_retries_on_next_import(self):
        self.asset.write_bytes(png()[:-4])
        library = self.library()
        result = library.collect(self.state, 'ABC123')
        self.assertEqual(result['added'], 0)
        self.assertEqual(len(result['errors']), 1)
        self.asset.write_bytes(png())
        self.assertEqual(library.collect(self.state, 'ABC123')['added'], 1)

    def test_corrupt_asset_not_silently_accepted(self):
        library = self.library()
        library.collect(self.state, 'ABC123')
        Path(library.search()[0]['asset']).write_bytes(b'corrupt')
        self.assertEqual(len(library.collect(self.state, 'ABC123')['errors']), 1)

    def test_annotations_search_and_restart(self):
        with Library(self.root / 'library') as library:
            library.collect(self.state, 'ABC123')
            item_id = library.search()[0]['id']
            library.annotate(item_id, '波函数 100%', [' 物理 ', '物理', 'Quantum'])
        with Library(self.root / 'library') as library:
            self.assertEqual(len(library.search('波函数')), 1)
            self.assertEqual(len(library.search('quantum')), 1)
            self.assertEqual(len(library.search('%')), 1)
            self.assertEqual(library.search('_'), [])
            self.assertEqual(len(library.search()[0]['tags']), 2)
            with self.assertRaises(ValueError):
                library.annotate(999, '', [])

    def test_invalid_group_cannot_escape_source(self):
        library = self.library()
        for group in ('../ABC123', 'ABC123/..', 'MISSING'):
            with self.assertRaises(ValueError):
                library.collect(self.state, group)

    def test_source_files_unchanged(self):
        before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.state.rglob('*') if p.is_file()}
        inspect_source(self.state)
        self.library().collect(self.state, 'ABC123')
        after = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.state.rglob('*') if p.is_file()}
        self.assertEqual(before, after)


if __name__ == '__main__':
    unittest.main()
