"""Verified, streaming portable-release downloads. Never executes downloaded code."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading
from urllib.parse import urlsplit
from urllib.request import build_opener, HTTPRedirectHandler, Request

SITE = 'https://snipboard.violet-whale-0649.chatgpt.site'
REPOSITORY = 'https://github.com/cjr050127-bit/SnipBoard'
MANIFEST_URLS = (SITE + '/updates/latest.json',
                 'https://raw.githubusercontent.com/cjr050127-bit/SnipBoard/main/updates/latest.json')
MAX_BYTES = 512 * 1024 * 1024
TIMEOUT = 12
BLOCK_BYTES = 4 * 1024 * 1024


class UpdateError(Exception):
    pass


class DownloadCancelled(UpdateError):
    pass


def version_tuple(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{1,4}\.\d{1,4}\.\d{1,4}', value):
        raise UpdateError('版本信息格式不正确')
    return tuple(map(int, value.split('.')))


def trusted_url(url, *, redirect=False):
    if not isinstance(url, str):
        raise UpdateError('下载地址不正确')
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or parsed.username or parsed.password or
            parsed.port not in (None, 443) or parsed.fragment):
        raise UpdateError('更新源必须使用受信任的 HTTPS 地址')
    host = parsed.hostname
    path = parsed.path
    allowed = (host == urlsplit(SITE).hostname and path.startswith(('/downloads/', '/updates/')) or
               host == 'github.com' and path.startswith('/cjr050127-bit/SnipBoard/releases/download/') or
               host == 'raw.githubusercontent.com' and path == '/cjr050127-bit/SnipBoard/main/updates/latest.json')
    if redirect and host == 'release-assets.githubusercontent.com':
        allowed = True
    if not allowed:
        raise UpdateError('更新源地址不在允许的范围内')
    return url


class _Redirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        trusted_url(newurl, redirect=True)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def open_url(url, *, headers=None):
    trusted_url(url)
    response = build_opener(_Redirects()).open(
        Request(url, headers={'User-Agent': 'SnipBoard-Updater', 'Accept-Encoding': 'identity', **(headers or {})}), timeout=TIMEOUT)
    try:
        trusted_url(response.geturl(), redirect=True)
    except Exception:
        response.close()
        raise
    return response


@dataclass(frozen=True)
class Release:
    version: str
    filename: str
    size: int
    sha256: str
    urls: tuple[str, ...]
    notes: str

    @classmethod
    def parse(cls, data):
        if not isinstance(data, dict) or data.get('schema') != 1:
            raise UpdateError('更新信息格式不正确')
        version = data.get('version')
        version_tuple(version)
        filename = data.get('filename')
        if filename != f'SnipBoard-{version}-Windows-x64.zip':
            raise UpdateError('更新包名称不正确')
        size, digest = data.get('size'), data.get('sha256')
        if type(size) is not int or not 0 < size <= MAX_BYTES:
            raise UpdateError('更新包大小不正确')
        if not isinstance(digest, str) or not re.fullmatch('[0-9a-fA-F]{64}', digest):
            raise UpdateError('更新包缺少有效的 SHA256 校验信息')
        urls = data.get('urls')
        if not isinstance(urls, list) or not 1 <= len(urls) <= 3:
            raise UpdateError('更新包下载地址不正确')
        for url in urls:
            trusted_url(url)
            parts = urlsplit(url)
            if parts.hostname == urlsplit(SITE).hostname:
                expected = '/downloads/' + filename
            elif parts.hostname == 'github.com':
                expected = f'/cjr050127-bit/SnipBoard/releases/download/v{version}/{filename}'
            else:
                raise UpdateError('更新包下载地址不正确')
            if parts.path != expected or parts.query:
                raise UpdateError('更新包地址与版本不匹配')
        notes = data.get('notes', '')
        if not isinstance(notes, str) or len(notes) > 12000:
            raise UpdateError('更新说明格式不正确')
        return cls(version, filename, size, digest.lower(), tuple(urls), notes)


def _cancelled(cancel):
    if cancel.is_set():
        raise DownloadCancelled('已取消下载')


def check_release(cancel=None, opener=open_url):
    cancel = cancel or threading.Event()
    for url in MANIFEST_URLS:
        _cancelled(cancel)
        try:
            with opener(url) as response:
                raw = response.read(65537)
            _cancelled(cancel)
            if len(raw) > 65536:
                raise UpdateError('更新信息过大')
            return Release.parse(json.loads(raw))
        except DownloadCancelled:
            raise
        except Exception:
            continue
    raise UpdateError('暂时无法连接更新服务器，请稍后重试；也可从官网下载。')


def _digest(path, cancel):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        while chunk := stream.read(256 * 1024):
            _cancelled(cancel)
            value.update(chunk)
    return value.hexdigest()


def _download_blocks(url, release, output, cancel, progress, opener):
    """Bounded range requests survive proxies that truncate large responses."""
    digest = hashlib.sha256()
    for offset in range(0, release.size, BLOCK_BYTES):
        end = min(offset + BLOCK_BYTES, release.size) - 1
        expected = end - offset + 1
        for attempt in range(2):
            _cancelled(cancel)
            try:
                with opener(url, headers={'Range': f'bytes={offset}-{end}'}) as response:
                    if (response.status != 206 or response.headers.get('Content-Range') !=
                            f'bytes {offset}-{end}/{release.size}'):
                        raise UpdateError('服务器未正确返回下载分段')
                    declared = response.headers.get('Content-Length')
                    if declared is not None and int(declared) != expected:
                        raise UpdateError('下载分段大小不匹配')
                    parts, received = [], 0
                    while True:
                        _cancelled(cancel)
                        chunk = response.read(min(256 * 1024, expected - received + 1))
                        if not chunk:
                            break
                        received += len(chunk)
                        if received > expected:
                            raise UpdateError('下载分段超过预期大小')
                        parts.append(chunk)
                        progress(offset + received, release.size)
                    if received != expected:
                        raise UpdateError('下载分段不完整')
                    block = b''.join(parts)
                _cancelled(cancel)
                break
            except DownloadCancelled:
                raise
            except Exception:
                if attempt:
                    raise
                progress(offset, release.size)
        output.write(block)
        digest.update(block)
    return release.size, digest.hexdigest()


def download_release(release, root, cancel, progress=lambda received, total: None,
                     source_changed=lambda index: None, opener=open_url):
    # Each release gets an app-owned directory; never extract over the running app.
    folder = Path(root) / release.version
    folder.mkdir(parents=True, exist_ok=True)
    destination = folder / release.filename
    _cancelled(cancel)
    if destination.exists():
        if destination.stat().st_size == release.size and _digest(destination, cancel) == release.sha256:
            progress(release.size, release.size)
            return destination
        raise UpdateError('下载目录已有同名文件且校验不匹配，请移走该文件后重试。')
    last_error = None
    for index, url in enumerate(release.urls):
        _cancelled(cancel)
        source_changed(index)
        received = 0
        digest = hashlib.sha256()
        handle, name = tempfile.mkstemp(prefix='.snipboard-', suffix='.part', dir=folder)
        temporary = Path(name)
        try:
            with os.fdopen(handle, 'wb') as output:
                progress(0, release.size)
                if urlsplit(url).hostname == urlsplit(SITE).hostname:
                    received, actual_hash = _download_blocks(url, release, output, cancel, progress, opener)
                else:
                    with opener(url) as response:
                        declared = response.headers.get('Content-Length')
                        if declared is not None and int(declared) != release.size:
                            raise UpdateError('服务器返回的文件大小不匹配')
                        while True:
                            _cancelled(cancel)
                            chunk = response.read(256 * 1024)
                            if not chunk:
                                break
                            received += len(chunk)
                            if received > release.size:
                                raise UpdateError('下载文件超过预期大小')
                            output.write(chunk)
                            digest.update(chunk)
                            progress(received, release.size)
                    actual_hash = digest.hexdigest()
                output.flush()
                os.fsync(output.fileno())
            _cancelled(cancel)
            if received != release.size or actual_hash != release.sha256:
                raise UpdateError('下载文件不完整或 SHA256 校验失败')
            # rename does not overwrite an existing destination on Windows.
            if destination.exists():
                raise UpdateError('同名下载文件已存在，请检查下载目录。')
            temporary.rename(destination)
            return destination
        except DownloadCancelled:
            raise
        except OSError as error:
            # Network failures can use the fallback; local write failures should not.
            if error.errno in (13, 28):
                raise UpdateError('无法写入下载目录，请检查空间和访问权限。') from error
            last_error = error
        except Exception as error:
            last_error = error
        finally:
            temporary.unlink(missing_ok=True)
    if isinstance(last_error, UpdateError):
        raise last_error
    raise UpdateError('下载未完成，两个下载源均暂不可用。请稍后重试或访问官网。') from last_error
