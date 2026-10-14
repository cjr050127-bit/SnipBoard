"""Verify and package a local release without changing published update feeds."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import zipfile

from snipboard.updates import REPOSITORY, SITE, Release


MANIFEST = 'FILE_MANIFEST.json'


def _sha256(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f'Duplicate manifest key: {key}')
        result[key] = value
    return result


def _linked(path: Path) -> bool:
    return path.is_symlink() or getattr(path, 'is_junction', lambda: False)()


def _inventory(folder: Path) -> set[str]:
    names = set()
    for path in folder.rglob('*'):
        if _linked(path) or not path.resolve().is_relative_to(folder):
            raise ValueError(f'Linked or escaped distribution path: {path}')
        if path.is_file():
            names.add(path.relative_to(folder).as_posix())
        elif not path.is_dir():
            raise ValueError(f'Unsupported distribution entry: {path}')
    return names


def _verified_files(folder: Path) -> tuple[dict, bytes]:
    manifest_path = folder / MANIFEST
    if _linked(manifest_path):
        raise ValueError('The file manifest must not be linked')
    raw = manifest_path.read_bytes()
    files = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_keys)
    if not isinstance(files, dict) or not files:
        raise ValueError('Expected a nonempty file manifest')
    folded = set()
    for name, metadata in files.items():
        if (not isinstance(name, str) or '\\' in name or ':' in name or
                any(ord(character) < 32 for character in name) or
                any(part in ('', '.', '..') for part in name.split('/')) or
                name.casefold() == MANIFEST.casefold()):
            raise ValueError(f'Invalid manifest path: {name}')
        if name.casefold() in folded:
            raise ValueError(f'Conflicting manifest path: {name}')
        folded.add(name.casefold())
        if (not isinstance(metadata, dict) or type(metadata.get('bytes')) is not int or
                metadata['bytes'] < 0 or not isinstance(metadata.get('sha256'), str) or
                not re.fullmatch(r'[0-9a-fA-F]{64}', metadata['sha256'])):
            raise ValueError(f'Invalid manifest metadata: {name}')
        path = folder / name
        if not path.resolve().is_relative_to(folder) or _linked(path):
            raise ValueError(f'Escaped manifest path: {name}')
        if not path.is_file() or path.stat().st_size != metadata['bytes']:
            raise ValueError(f'Missing file or size mismatch: {name}')
        if _sha256(path) != metadata['sha256'].lower():
            raise ValueError(f'SHA256 mismatch: {name}')
    expected = set(files) | {MANIFEST}
    actual = _inventory(folder)
    if actual != expected:
        raise ValueError(f'Distribution file set differs: extra={sorted(actual - expected)}, '
                         f'missing={sorted(expected - actual)}')
    return files, raw


def package_release(project: Path, version: str) -> dict:
    """Create a verified ZIP, checksum and local-only staging records.

    No network calls are made. In particular, updates/latest.json is neither
    created nor modified, and prospective release URLs only go into build/.
    """
    if not isinstance(version, str) or not re.fullmatch(r'\d{1,4}\.\d{1,4}\.\d{1,4}', version):
        raise ValueError('Invalid release version')
    project = Path(project).resolve()
    dist = project / 'dist'
    folder = dist / f'SnipBoard-{version}'
    build = project / 'build'
    for path in (dist, folder, build):
        if _linked(path) or not path.resolve().is_relative_to(project):
            raise ValueError(f'Linked or escaped release directory: {path}')
    if not folder.is_dir():
        raise ValueError(f'Missing distribution directory: {folder}')
    folder = folder.resolve()
    files, manifest_bytes = _verified_files(folder)
    notes = (project / 'docs' / f"RELEASE_{version.replace('.', '_')}.md").read_text(encoding='utf-8')
    target = dist / f'SnipBoard-{version}-Windows-x64.zip'
    checksum = Path(str(target) + '.sha256')
    verification_path = build / f'release-{version}-verification.json'
    staged_path = build / f'release-{version}-staged-update.json'
    for path in (target, checksum, verification_path, staged_path):
        if _linked(path):
            raise ValueError(f'Linked release output: {path}')

    descriptor, temporary_name = tempfile.mkstemp(prefix='.snipboard-release-', suffix='.zip', dir=dist)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for name in sorted(files):
                archive.write(folder / name, f'{folder.name}/{name}')
            archive.writestr(f'{folder.name}/{MANIFEST}', manifest_bytes)
        with zipfile.ZipFile(temporary) as archive:
            expected = {f'{folder.name}/{name}' for name in files} | {f'{folder.name}/{MANIFEST}'}
            if len(archive.infolist()) != len(expected) or set(archive.namelist()) != expected:
                raise ValueError('ZIP entries differ from the verified distribution')
            if archive.testzip() is not None:
                raise ValueError('ZIP CRC verification failed')
            for name, metadata in files.items():
                entry = f'{folder.name}/{name}'
                if archive.getinfo(entry).file_size != metadata['bytes']:
                    raise ValueError(f'ZIP size mismatch: {name}')
                with archive.open(entry) as stream:
                    digest = hashlib.file_digest(stream, 'sha256').hexdigest()
                if digest != metadata['sha256'].lower():
                    raise ValueError(f'ZIP SHA256 mismatch: {name}')
            if archive.read(f'{folder.name}/{MANIFEST}') != manifest_bytes:
                raise ValueError('ZIP file manifest differs from the verified manifest')
        # Fail if another process changed a build input while it was packaged.
        final_files, final_manifest = _verified_files(folder)
        if final_files != files or final_manifest != manifest_bytes:
            raise ValueError('Distribution changed during packaging')
        sha = _sha256(temporary)
        size = temporary.stat().st_size
        staged = {
            'schema': 1, 'version': version, 'filename': target.name,
            'size': size, 'sha256': sha,
            'urls': [SITE + f'/-/releases/download/v{version}/' + target.name,
                     REPOSITORY + f'/releases/download/v{version}/' + target.name],
            'notes': notes,
        }
        Release.parse(staged)
        record = {
            'schema': 1, 'version': version, 'published': False,
            'verified_files': len(files), 'filename': target.name,
            'zip_bytes': size, 'sha256': sha,
            'manifest_sha256': hashlib.sha256(manifest_bytes).hexdigest(),
        }
        build.mkdir(exist_ok=True)
        temporary.replace(target)
        checksum.write_text(f'{sha}  {target.name}\n', encoding='ascii')
        verification_path.write_text(json.dumps(record, indent=2) + '\n', encoding='utf-8')
        staged_path.write_text(json.dumps(staged, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        return record
    finally:
        temporary.unlink(missing_ok=True)


def main() -> None:
    from snipboard import __version__

    record = package_release(Path(__file__).resolve().parents[1], __version__)
    print('LOCAL_ZIP_VERIFIED', record['version'], record['verified_files'],
          record['zip_bytes'], record['sha256'])


if __name__ == '__main__':
    main()