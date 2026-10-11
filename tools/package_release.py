"""Package the current verified build and emit the shared updater manifest."""
from pathlib import Path
import hashlib
import json
import zipfile
from snipboard import __version__
from snipboard.updates import SITE, REPOSITORY, Release

root = Path(__file__).resolve().parents[1]
folder = root / 'dist' / f'SnipBoard-{__version__}'
target = folder.parent / f'SnipBoard-{__version__}-Windows-x64.zip'
files = json.loads((folder / 'FILE_MANIFEST.json').read_text(encoding='utf-8'))
for name, metadata in files.items():
    path = folder / name
    assert path.stat().st_size == metadata['bytes'], name
    assert hashlib.sha256(path.read_bytes()).hexdigest() == metadata['sha256'], name
with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
    for path in sorted(folder.rglob('*')):
        if path.is_file():
            archive.write(path, path.relative_to(folder.parent))
with zipfile.ZipFile(target) as archive:
    assert archive.testzip() is None
sha = hashlib.sha256(target.read_bytes()).hexdigest()
Path(str(target) + '.sha256').write_text(sha + '  ' + target.name + '\n', encoding='ascii')
data = {'schema': 1, 'version': __version__, 'filename': target.name,
        'size': target.stat().st_size, 'sha256': sha,
        'urls': [SITE + '/downloads/' + target.name,
                 REPOSITORY + f'/releases/download/v{__version__}/' + target.name],
        'notes': (root / 'docs' / f"RELEASE_{__version__.replace('.', '_')}.md").read_text(encoding='utf-8')}
Release.parse(data)
(root / 'updates').mkdir(exist_ok=True)
(root / 'updates/latest.json').write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
record = dict(version=__version__, verified_files=len(files), zip_bytes=target.stat().st_size, sha256=sha)
(root / 'build' / f'release-{__version__}-verification.json').write_text(json.dumps(record, indent=2), encoding='utf-8')
print('ZIP_VERIFIED', len(files), target.stat().st_size, sha)
