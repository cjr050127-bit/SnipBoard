"""Read-only integrity inspection and verified recovery into a new directory."""
from pathlib import Path
import hashlib
import os
import re
import shutil
import sqlite3
import tempfile
import zipfile

from PIL import Image

DIGEST = re.compile(r'[0-9a-f]{64}')


def inspect_library(catalog, rebuild=False, cancelled=lambda: False, progress=lambda *args: None):
    report = dict(database=[], missing=[], corrupt=[], thumbnails=[], rebuilt=0, checked=0, cancelled=False)
    report['database'] = [r[0] for r in catalog.db.execute('PRAGMA quick_check') if r[0] != 'ok']
    report['database'] += [str(tuple(r)) for r in catalog.db.execute('PRAGMA foreign_key_check')]
    digests = [r[0] for r in catalog.db.execute('SELECT DISTINCT digest FROM items')]
    for index, digest in enumerate(digests, 1):
        if cancelled():
            report['cancelled'] = True
            break
        if not DIGEST.fullmatch(digest):
            report['corrupt'].append(str(digest))
            continue
        asset = catalog.root / 'assets' / f'{digest}.png'
        if not asset.is_file():
            report['missing'].append(str(asset))
        else:
            try:
                with asset.open('rb') as stream:
                    if hashlib.file_digest(stream, 'sha256').hexdigest() != digest:
                        raise ValueError('图片校验值不匹配')
                with Image.open(asset) as image:
                    image.verify()
                thumb = catalog.thumbnails / f'{digest}-800.png'
                try:
                    with Image.open(thumb) as image:
                        image.verify()
                except (OSError, ValueError, SyntaxError):
                    report['thumbnails'].append(str(thumb))
                if rebuild:
                    catalog.ensure_feature(digest, force=True)
                    report['rebuilt'] += 1
            except (OSError, ValueError, SyntaxError, Image.DecompressionBombError) as error:
                report['corrupt'].append(f'{asset}: {error}')
        report['checked'] += 1
        progress(index, len(digests))
    return report


def restore_backup(archive, destination, max_version, cancelled=lambda: False):
    """Validate first; publish a new library only after every required asset is verified."""
    archive, destination = Path(archive).resolve(), Path(destination).absolute()
    if destination.exists():
        raise FileExistsError('恢复目标已存在，请选择一个新文件夹')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.snipboard-restore-', dir=destination.parent) as temporary:
        stage = Path(temporary) / 'library'
        stage.mkdir()
        with zipfile.ZipFile(archive) as source:
            entries = source.infolist()
            names = [item.filename for item in entries]
            if len(names) != len(set(names)) or names.count('library.sqlite3') != 1:
                raise ValueError('备份目录无效或包含重复项目')
            for entry in entries:
                name = entry.filename
                if name != 'library.sqlite3' and not re.fullmatch(r'assets/[0-9a-f]{64}\.png', name):
                    raise ValueError('备份包含不支持的路径')
                if entry.file_size > 512 * 1024 * 1024:
                    raise ValueError('单个备份项目超过恢复大小限制')
            if sum(item.file_size for item in entries) > shutil.disk_usage(destination.parent).free:
                raise OSError('恢复位置磁盘空间不足')
            for entry in entries:
                if cancelled():
                    raise InterruptedError('恢复已取消')
                target = stage / entry.filename
                target.parent.mkdir(exist_ok=True)
                with source.open(entry) as incoming, target.open('xb') as output:
                    shutil.copyfileobj(incoming, output, length=1024 * 1024)
        database = stage / 'library.sqlite3'
        connection = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)
        try:
            version = connection.execute('PRAGMA user_version').fetchone()[0]
            if not 1 <= version <= max_version:
                raise ValueError('备份版本不受此版本程序支持')
            if connection.execute('PRAGMA quick_check').fetchall() != [('ok',)] or connection.execute('PRAGMA foreign_key_check').fetchall():
                raise ValueError('备份数据库校验失败')
            digests = [r[0] for r in connection.execute('SELECT DISTINCT digest FROM items')]
        finally:
            connection.close()
        for digest in digests:
            if cancelled():
                raise InterruptedError('恢复已取消')
            if not isinstance(digest, str) or not DIGEST.fullmatch(digest):
                raise ValueError('备份图片标识无效')
            asset = stage / 'assets' / f'{digest}.png'
            with asset.open('rb') as stream:
                if hashlib.file_digest(stream, 'sha256').hexdigest() != digest:
                    raise ValueError('备份图片校验失败')
            with Image.open(asset) as image:
                image.verify()
        # Backups contain originals and metadata, not disposable thumbnail caches.
        from .catalog import Catalog
        with Catalog(stage) as restored:
            for digest in digests:
                if cancelled():
                    raise InterruptedError('恢复已取消')
                restored.ensure_feature(digest)
        # Windows rename refuses an existing target; the active library is never replaced.
        if destination.exists():
            raise FileExistsError('恢复目标已存在')
        os.rename(stage, destination)
    return dict(path=str(destination), images=len(digests), version=version)
