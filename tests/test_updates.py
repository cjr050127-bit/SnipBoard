import hashlib
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.request import Request

from snipboard.updates import (Release, SITE, LEGACY_SITE, REPOSITORY, UpdateError, DownloadCancelled,
                               version_tuple, trusted_url, check_release, download_release,
                               MANIFEST_URLS, _Redirects)
from snipboard.updates import BLOCK_BYTES

PAYLOAD = b'portable release test' * 20000


def manifest(version='0.6.99'):
    name = f'SnipBoard-{version}-Windows-x64.zip'
    return {'schema': 1, 'version': version, 'filename': name, 'size': len(PAYLOAD),
            'sha256': hashlib.sha256(PAYLOAD).hexdigest(),
            'urls': [SITE + f'/-/releases/download/v{version}/' + name,
                     REPOSITORY + f'/releases/download/v{version}/' + name],
            'notes': '测试更新说明'}


class Response(io.BytesIO):
    def __init__(self, payload=PAYLOAD, size=None):
        super().__init__(payload)
        self.status = 206
        self.headers = {'Content-Length': str(len(payload) if size is None else size),
                        'Content-Range': f'bytes 0-{len(PAYLOAD)-1}/{len(PAYLOAD)}'}


class UpdatesTests(unittest.TestCase):
    def test_cnb_is_preferred_manifest_and_legacy_release_remains_usable(self):
        self.assertEqual(MANIFEST_URLS[0], SITE + '/-/git/raw/main/updates/latest.json')
        data = manifest()
        data['urls'][0] = LEGACY_SITE + '/downloads/' + data['filename']
        release = Release.parse(data)
        calls = []
        def opener(url, headers=None):
            calls.append((url, headers))
            return Response()
        with tempfile.TemporaryDirectory() as directory:
            result = download_release(release, directory, threading.Event(), opener=opener)
            self.assertEqual(result.read_bytes(), PAYLOAD)
        self.assertEqual(calls[0][1], {'Range': f'bytes=0-{len(PAYLOAD)-1}'})

    def test_cnb_manifest_rejects_other_projects_mismatched_versions_and_asset_urls(self):
        valid = manifest()['urls'][0]
        for url in [valid.replace('/snipbroad-jrc/', '/someone-else/'),
                    valid.replace('/SnipBoard/', '/OtherProject/'),
                    valid.replace('/v0.6.99/', '/v0.6.98/'),
                    valid.replace('/v0.6.99/', '/main/'),
                    valid + '?token=unexpected', valid + '#fragment',
                    valid.replace('cnb.cool', 'cnb.cool:444'),
                    valid.replace('cnb.cool', 'cnb.cool:invalid'),
                    valid.replace('cnb.cool', 'user@cnb.cool'),
                    'https://asset.cnb.cool/unverified/package.zip']:
            data = manifest()
            data['urls'] = [url]
            with self.assertRaises(UpdateError, msg=url):
                Release.parse(data)

    def test_cnb_asset_redirect_retains_range_and_rejects_other_asset_hosts(self):
        request = Request(manifest()['urls'][0], headers={'Range': 'bytes=0-99'})
        asset = 'https://asset.cnb.cool/releases/test.zip?signature=abc'
        redirected = _Redirects().redirect_request(request, None, 302, '', {}, asset)
        self.assertEqual(redirected.full_url, asset)
        self.assertEqual(redirected.get_header('Range'), 'bytes=0-99')
        with self.assertRaises(UpdateError):
            trusted_url(asset)
        for url in [asset.replace('asset.cnb.cool', 'asset.cnb.cool.evil.example'),
                    asset.replace('asset.cnb.cool', 'other.cnb.cool'),
                    asset.replace('https://', 'http://'),
                    asset.replace('asset.cnb.cool', 'user@asset.cnb.cool'),
                    asset.replace('asset.cnb.cool', 'asset.cnb.cool:444')]:
            with self.assertRaises(UpdateError):
                _Redirects().redirect_request(request, None, 302, '', {}, url)

    def test_truncated_range_retries_block_and_continues_without_duplicate_bytes(self):
        payload = b'B' * (BLOCK_BYTES + 4001)
        data = manifest()
        data.update(size=len(payload), sha256=hashlib.sha256(payload).hexdigest())
        release, calls = Release.parse(data), []
        def opener(url, headers=None):
            value = headers['Range']
            start, end = map(int, value.removeprefix('bytes=').split('-'))
            calls.append((start, end))
            raw = payload[start:end+1]
            if len(calls) == 1:
                raw = raw[:100]
            response = Response(raw, size=end-start+1)
            response.headers['Content-Range'] = f'bytes {start}-{end}/{len(payload)}'
            return response
        with tempfile.TemporaryDirectory() as directory:
            result = download_release(release, directory, threading.Event(), opener=opener)
            self.assertEqual(result.read_bytes(), payload)
            self.assertEqual(calls, [(0, BLOCK_BYTES-1), (0, BLOCK_BYTES-1), (BLOCK_BYTES, len(payload)-1)])

    def test_incorrect_range_response_falls_back_without_publishing_wrong_bytes(self):
        release, sources = Release.parse(manifest()), []
        def opener(url, headers=None):
            response = Response()
            if headers:
                response.headers['Content-Range'] = 'bytes 1-20/400000'
            return response
        with tempfile.TemporaryDirectory() as directory:
            result = download_release(release, directory, threading.Event(), source_changed=sources.append, opener=opener)
            self.assertEqual(result.read_bytes(), PAYLOAD)
            self.assertEqual(sources, [0, 1])

    def test_version_comparison_is_numeric_and_rejects_invalid(self):
        self.assertGreater(version_tuple('0.6.10'), version_tuple('0.6.9'))
        for value in ['v0.6.4', '0.6', '0.6.4/../file', None, '0.6.4-beta']:
            with self.assertRaises(UpdateError):
                version_tuple(value)

    def test_manifest_rejects_paths_untrusted_sources_and_missing_hash(self):
        for field, value in [('filename', '../update.exe'), ('size', True), ('size', -1),
                             ('sha256', ''), ('urls', ['https://example.com/update.zip']),
                             ('urls', [SITE + '/-/releases/download/v0.6.99/wrong.zip']), ('notes', '<x>' * 5000)]:
            data = manifest()
            data[field] = value
            with self.assertRaises(UpdateError, msg=field):
                Release.parse(data)

    def test_url_guard_rejects_credentials_plain_http_and_wrong_repository(self):
        for url in ['http://github.com/x', 'file:///tmp/update', SITE.replace('https://', 'https://x@') + '/-/git/raw/main/updates/latest.json',
                    'https://github.com/other/repo/releases/download/v1/a.zip',
                    'https://raw.githubusercontent.com/other/repo/main/updates/latest.json']:
            with self.assertRaises(UpdateError):
                trusted_url(url)
        self.assertTrue(trusted_url('https://release-assets.githubusercontent.com/a?signature=x', redirect=True))
        with self.assertRaises(UpdateError):
            _Redirects().redirect_request(None, None, 302, '', {}, 'https://evil.example/a')

    def test_manifest_falls_back_and_limits_response_size(self):
        calls = []
        def opener(url, headers=None):
            calls.append(url)
            return Response(b'x' * 65537 if len(calls) == 1 else json.dumps(manifest()).encode())
        self.assertEqual(check_release(opener=opener).version, '0.6.99')
        self.assertEqual(calls, list(MANIFEST_URLS))

    def test_cancelled_check_does_not_connect(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(DownloadCancelled):
            check_release(cancel, opener=lambda _: self.fail('should not connect'))

    def test_stream_download_verifies_hash_and_reuses_verified_file(self):
        release = Release.parse(manifest())
        with tempfile.TemporaryDirectory() as directory:
            progress = []
            result = download_release(release, directory, threading.Event(),
                lambda n, total: progress.append((n, total)), opener=lambda url, headers=None: Response())
            self.assertEqual(result.read_bytes(), PAYLOAD)
            self.assertEqual(progress[-1], (len(PAYLOAD), len(PAYLOAD)))
            self.assertEqual(len(list(result.parent.iterdir())), 1)
            again = download_release(release, directory, threading.Event(),
                                     opener=lambda _: self.fail('verified file should be reused'))
            self.assertEqual(result, again)

    def test_bad_primary_download_is_removed_before_fallback(self):
        release = Release.parse(manifest())
        sources = []
        def opener(url, headers=None):
            return Response(b'corrupt', size=release.size) if url == release.urls[0] else Response()
        with tempfile.TemporaryDirectory() as directory:
            result = download_release(release, directory, threading.Event(),
                                      source_changed=sources.append, opener=opener)
            self.assertEqual(result.read_bytes(), PAYLOAD)
            self.assertEqual(sources, [0, 1])
            self.assertFalse(list(Path(directory).rglob('*.part')))

    def test_bad_hash_never_publishes_a_zip(self):
        data = manifest()
        data['sha256'] = '0' * 64
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(UpdateError):
                download_release(Release.parse(data), directory, threading.Event(), opener=lambda url, headers=None: Response())
            self.assertFalse(list(Path(directory).rglob('*.zip')))
            self.assertFalse(list(Path(directory).rglob('*.part')))

    def test_cancel_mid_download_cleans_owned_partial_without_fallback(self):
        cancel = threading.Event()
        sources = []
        def progress(received, total):
            if received:
                cancel.set()
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(DownloadCancelled):
                download_release(Release.parse(manifest()), directory, cancel, progress,
                                 sources.append, opener=lambda url, headers=None: Response())
            self.assertEqual(sources, [0])
            self.assertFalse(list(Path(directory).rglob('*.part')))
            self.assertFalse(list(Path(directory).rglob('*.zip')))

    def test_existing_unverified_file_is_preserved(self):
        release = Release.parse(manifest())
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / release.version / release.filename
            destination.parent.mkdir()
            destination.write_bytes(b'keep me')
            with self.assertRaises(UpdateError):
                download_release(release, directory, threading.Event(), opener=lambda url, headers=None: Response())
            self.assertEqual(destination.read_bytes(), b'keep me')


import test_qt
from snipboard.qt_app import Controller
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest


class UpdateUITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.controller = Controller(test_qt.APP, None, Path(self.temp.name) / 'library', native=False)
        self.controller.timer.stop()
        self.window = self.controller.new_library()
        self.service = self.controller.updates
        self.service.root = Path(self.temp.name) / 'downloads'
        self.service.checker = lambda _: Release.parse(manifest())
        test_qt.drain(self.controller)

    def tearDown(self):
        self.controller.quit()
        test_qt.drain(self.controller)
        self.controller._finish_shutdown()
        if self.service.dialog:
            self.service.dialog.deleteLater()
        test_qt.APP.processEvents()
        self.temp.cleanup()

    def test_auto_check_is_nonmodal_and_banner_opens_downloader(self):
        self.service.automatic_check()
        test_qt.drain(self.controller)
        self.assertIsNone(self.service.dialog)
        self.assertTrue(self.window.update_button.isVisible())
        QTest.mouseClick(self.window.update_button, Qt.LeftButton)
        self.assertTrue(self.service.dialog.isVisible())
        self.assertFalse(self.service.dialog.isModal())
        self.assertTrue(self.service.dialog.download_button.isEnabled())

    def test_disabled_and_skipped_auto_check_but_manual_check_still_works(self):
        self.controller.catalog.set_setting('updates.enabled', False)
        self.service.automatic_check()
        self.assertFalse(self.service.checking)
        self.controller.catalog.set_setting('updates.enabled', True)
        self.controller.catalog.set_setting('updates.skipped', '0.6.99')
        self.service.automatic_check()
        test_qt.drain(self.controller)
        self.assertFalse(self.window.update_button.isVisible())
        self.service.manual_check()
        test_qt.drain(self.controller)
        self.assertTrue(self.window.update_button.isVisible())
        self.service.skip()
        self.assertFalse(self.window.update_button.isVisible())

    def test_download_buttons_completion_and_open_folder(self):
        self.service.downloader = lambda release, root, cancel, progress, source: download_release(
            release, root, cancel, progress, source, opener=lambda url, headers=None: Response())
        self.service.manual_check()
        test_qt.drain(self.controller)
        dialog = self.service.dialog
        QTest.mouseClick(dialog.download_button, Qt.LeftButton)
        self.assertTrue(self.service.downloading)
        test_qt.drain(self.controller)
        self.assertTrue(self.service.path.is_file())
        self.assertIn('校验通过', dialog.status.text())
        self.assertTrue(dialog.folder_button.isVisible())
        self.assertTrue(dialog.instructions.isVisible())
        with patch('snipboard.qt_updates.QDesktopServices.openUrl') as opened:
            QTest.mouseClick(dialog.folder_button, Qt.LeftButton)
            self.assertEqual(Path(opened.call_args.args[0].toLocalFile()), self.service.path.parent)

    def test_cancel_retry_and_check_after_cancel(self):
        def downloader(release, root, cancel, progress, source):
            cancel.wait(3)
            raise DownloadCancelled()
        self.service.downloader = downloader
        self.service.manual_check()
        test_qt.drain(self.controller)
        self.service.download()
        QTest.mouseClick(self.service.dialog.cancel_button, Qt.LeftButton)
        test_qt.drain(self.controller)
        self.assertFalse(self.service.downloading)
        self.assertIsNone(self.service.path)
        self.assertTrue(self.service.dialog.download_button.isEnabled())
        self.service.check(manual=True)
        test_qt.drain(self.controller)
        self.assertFalse(self.service.cancel.is_set())

    def test_check_failure_does_not_open_an_automatic_dialog(self):
        def failed(_):
            raise UpdateError('网络暂不可用')
        self.service.checker = failed
        self.service.automatic_check()
        test_qt.drain(self.controller)
        self.assertIsNone(self.service.dialog)
        self.assertFalse(self.window.update_button.isVisible())
        self.service.manual_check()
        test_qt.drain(self.controller)
        self.assertIn('网络暂不可用', self.service.dialog.status.text())

    def test_shutdown_cancels_download_and_does_not_access_closed_catalog(self):
        def downloader(release, root, cancel, progress, source):
            self.assertTrue(cancel.wait(3))
            raise DownloadCancelled()
        self.service.downloader = downloader
        self.service.manual_check()
        test_qt.drain(self.controller)
        self.service.download()
        self.controller.quit()
        test_qt.drain(self.controller)
        self.assertTrue(self.service.closed)
        self.assertFalse(self.service.downloading)
