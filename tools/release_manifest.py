"""Record exact build inputs, supplied notices, and the hashes of shipped files."""
from importlib.metadata import distribution
import hashlib
import json
from pathlib import Path
import shutil
import sys
from types import CodeType
from PyInstaller.archive.readers import ZlibArchiveReader

destination = Path(sys.argv[1]).resolve()
project = Path(__file__).resolve().parents[1]
if not destination.is_relative_to(project / 'dist') or not destination.is_dir():
    raise ValueError('Expected an existing project distribution directory')
def code_signature(code):
    return (code.co_code, code.co_names, code.co_varnames, code.co_argcount, code.co_kwonlyargcount,
            code.co_freevars, code.co_cellvars,
            tuple(code_signature(value) if isinstance(value, CodeType) else value for value in code.co_consts))
archive = ZlibArchiveReader(str(project / 'build' / 'SnipBoard-Qt' / 'PYZ-00.pyz'))
verified_modules = []
for path in (project / 'src' / 'snipboard').glob('*.py'):
    name = 'snipboard' if path.stem == '__init__' else 'snipboard.' + path.stem
    if name in archive.toc:
        if code_signature(archive.extract(name)) != code_signature(compile(path.read_text(encoding='utf-8'), str(path), 'exec')):
            raise RuntimeError(f'Packaged module differs from source: {name}')
        verified_modules.append(name)
(destination / 'SOURCE_VERIFICATION.json').write_text(json.dumps(verified_modules, indent=2), encoding='utf-8')
packages = []
for name in ['PySide6-Essentials', 'shiboken6', 'Pillow', 'numpy', 'PyInstaller', 'pyinstaller-hooks-contrib']:
    package = distribution(name)
    notices = []
    for filename in package.files or []:
        relative = Path(str(filename))
        if '..' in relative.parts or not any(word in str(relative).lower() for word in ('license', 'copying')):
            continue
        source = Path(package.locate_file(filename))
        if not source.is_file():
            continue
        target = destination / 'third-party' / name / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        notices.append(str(target.relative_to(destination)))
    packages.append(dict(name=name, version=package.version,
        license_metadata=package.metadata.get('License-Expression') or package.metadata.get('License'),
        notices=notices, build_only=name in ('PyInstaller', 'pyinstaller-hooks-contrib')))
python_license = Path(sys.base_prefix) / 'LICENSE.txt'
if python_license.exists():
    (destination / 'third-party' / 'Python').mkdir(parents=True, exist_ok=True)
    shutil.copyfile(python_license, destination / 'third-party' / 'Python' / 'LICENSE.txt')
(destination / 'DEPENDENCIES.json').write_text(json.dumps(dict(python=sys.version, packages=packages), indent=2), encoding='utf-8')
shutil.copytree(project / 'third-party', destination / 'third-party', dirs_exist_ok=True)
for name in ('LICENSE', 'THIRD_PARTY_NOTICES.md'):
    shutil.copyfile(project / name, destination / name)
records = {}
for path in sorted(destination.rglob('*')):
    if path.is_file() and path.name != 'FILE_MANIFEST.json':
        with path.open('rb') as stream:
            records[path.relative_to(destination).as_posix()] = dict(bytes=path.stat().st_size,
                sha256=hashlib.file_digest(stream, 'sha256').hexdigest())
(destination / 'FILE_MANIFEST.json').write_text(json.dumps(records, indent=2), encoding='utf-8')
print(f'MANIFEST_OK={len(records)} files')
