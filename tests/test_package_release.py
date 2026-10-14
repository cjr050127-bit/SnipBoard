"""Release artifacts must contain only verified build files and stay local."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from snipboard.updates import Release
from tools.package_release import package_release


class PackageReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.project = Path(self.temporary.name)
        self.version = '7.0.0'
        self.folder = self.project / 'dist' / 'SnipBoard-7.0.0'
        self.folder.mkdir(parents=True)
        self.payloads = {'SnipBoard.exe': b'example executable bytes',
                         '_internal/notice.txt': 'Example notice: 图片'.encode('utf-8')}
        self.manifest = {}
        for name, payload in self.payloads.items():
            path = self.folder / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
            self.manifest[name] = {'bytes': len(payload), 'sha256': hashlib.sha256(payload).hexdigest()}
        self.write_manifest()
        (self.project / 'docs').mkdir()
        (self.project / 'docs' / 'RELEASE_7_0_0.md').write_text('本地正式版，尚未发布。', encoding='utf-8')
        (self.project / 'updates').mkdir()
        self.feed = self.project / 'updates' / 'latest.json'
        self.feed_bytes = b'{"schema":1,"version":"0.6.6"}\r\n'
        self.feed.write_bytes(self.feed_bytes)

    def write_manifest(self):
        (self.folder / 'FILE_MANIFEST.json').write_text(json.dumps(self.manifest), encoding='utf-8')

    def test_builds_complete_crc_valid_zip_checksums_and_local_records(self):
        # Even a valid local release must not contact a service or update a feed.
        with patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('unexpected network')):
            result = package_release(self.project, self.version)
        target = self.project / 'dist' / 'SnipBoard-7.0.0-Windows-x64.zip'
        with zipfile.ZipFile(target) as archive:
            self.assertIsNone(archive.testzip())
            self.assertEqual({f'SnipBoard-7.0.0/{name}' for name in self.payloads} |
                             {'SnipBoard-7.0.0/FILE_MANIFEST.json'}, set(archive.namelist()))
            for name, payload in self.payloads.items():
                self.assertEqual(payload, archive.read(f'SnipBoard-7.0.0/{name}'))
            self.assertEqual(self.manifest, json.loads(archive.read('SnipBoard-7.0.0/FILE_MANIFEST.json')))
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        self.assertEqual(f'{digest}  {target.name}\n', Path(str(target) + '.sha256').read_text(encoding='ascii'))
        verification = json.loads((self.project / 'build' / 'release-7.0.0-verification.json').read_text())
        staged = json.loads((self.project / 'build' / 'release-7.0.0-staged-update.json').read_text(encoding='utf-8'))
        self.assertEqual(result, verification)
        self.assertFalse(verification['published'])
        self.assertEqual(2, verification['verified_files'])
        self.assertEqual(digest, verification['sha256'])
        self.assertEqual(target.stat().st_size, verification['zip_bytes'])
        self.assertEqual(1, verification['schema'])
        self.assertEqual(verification['schema'], staged['schema'])
        self.assertEqual(self.version, staged['version'])
        self.assertEqual(verification['version'], staged['version'])
        self.assertEqual(digest, staged['sha256'])
        self.assertEqual(target.stat().st_size, staged['size'])
        self.assertEqual(target.name, staged['filename'])
        parsed = Release.parse(staged)
        self.assertEqual(self.version, parsed.version)
        self.assertEqual(2, len(parsed.urls))
        self.assertEqual(self.feed_bytes, self.feed.read_bytes())
        self.assertFalse((self.folder / 'release-7.0.0-staged-update.json').exists())

    def test_does_not_create_a_missing_update_feed(self):
        self.feed.unlink()
        package_release(self.project, self.version)
        self.assertFalse(self.feed.exists())

    def test_rejects_modified_files_before_emitting_an_archive(self):
        path = self.folder / 'SnipBoard.exe'
        original = path.read_bytes()
        path.write_bytes(b'X' + original[1:])  # Same size; hashing must catch it.
        with self.assertRaisesRegex(ValueError, 'SHA256 mismatch'):
            package_release(self.project, self.version)
        self.assertFalse(list((self.project / 'dist').glob('*.zip')))
        self.assertEqual(self.feed_bytes, self.feed.read_bytes())

    def test_rejects_extra_private_files(self):
        extra = self.folder / 'library' / 'model-api-key.txt'
        extra.parent.mkdir()
        extra.write_text('dummy private value', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'Distribution file set differs'):
            package_release(self.project, self.version)
        self.assertFalse(list((self.project / 'dist').glob('*.zip')))

    def test_rejects_missing_files_and_wrong_declared_sizes(self):
        for problem in ('missing', 'size'):
            with self.subTest(problem=problem):
                path = self.folder / 'SnipBoard.exe'
                if problem == 'missing':
                    path.unlink()
                else:
                    path.write_bytes(self.payloads['SnipBoard.exe'])
                    self.manifest['SnipBoard.exe']['bytes'] += 1
                    self.write_manifest()
                with self.assertRaisesRegex(ValueError, 'Missing file or size mismatch'):
                    package_release(self.project, self.version)

    def test_rejects_manifest_path_traversal_and_windows_aliases(self):
        metadata = self.manifest['SnipBoard.exe']
        safe_manifest = dict(self.manifest)
        for name in ('../secret.txt', '/secret.txt', 'C:/secret.txt',
                     '_internal/../../secret.txt', '_internal\\secret.txt',
                     'SnipBoard.exe:secret', './SnipBoard.exe',
                     '_internal//notice.txt', 'FILE_MANIFEST.json'):
            with self.subTest(path=name):
                self.manifest = dict(safe_manifest)
                self.manifest[name] = metadata
                self.write_manifest()
                with self.assertRaisesRegex(ValueError, 'Invalid manifest path'):
                    package_release(self.project, self.version)

    def test_rejects_duplicate_manifest_keys_and_case_conflicts(self):
        raw_entry = json.dumps(self.manifest['SnipBoard.exe'])
        (self.folder / 'FILE_MANIFEST.json').write_text(
            '{"SnipBoard.exe":' + raw_entry + ',"SnipBoard.exe":' + raw_entry + '}', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'Duplicate manifest key'):
            package_release(self.project, self.version)
        self.manifest['snipboard.exe'] = self.manifest['SnipBoard.exe']
        self.write_manifest()
        with self.assertRaisesRegex(ValueError, 'Conflicting manifest path'):
            package_release(self.project, self.version)

    def test_rejects_version_paths_and_absent_builds(self):
        for version in ('../7.0.0', '7.0.0-beta', '7.0', '', None):
            with self.subTest(version=version):
                with self.assertRaisesRegex(ValueError, 'Invalid release version'):
                    package_release(self.project, version)
        with self.assertRaisesRegex(ValueError, 'Missing distribution directory'):
            package_release(self.project, '7.0.1')


if __name__ == '__main__':
    unittest.main()
