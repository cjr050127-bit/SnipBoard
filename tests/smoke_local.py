"""Opt-in real-source smoke test. Copies into an automatically cleaned temporary library."""
import hashlib
import json
from pathlib import Path
import tempfile

from snipboard.library import Library
from snipboard.source import discover, inspect_source


def fingerprint(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob('*') if p.is_file()}


def main():
    candidates = discover()
    if len(candidates) != 1:
        raise RuntimeError('Smoke test requires exactly one discoverable installation')
    source = candidates[0]
    snapshot = inspect_source(source)
    before = fingerprint(source / 'history')
    with tempfile.TemporaryDirectory(prefix='snipboard-smoke-') as directory:
        with Library(Path(directory)) as library:
            first = [library.collect(source, group['id'], group['name_candidate']) for group in snapshot['groups']]
            second = [library.collect(source, group['id']) for group in snapshot['groups']]
            if any(result['errors'] for result in first + second):
                raise RuntimeError(json.dumps(first + second))
            if any(result['added'] for result in second):
                raise RuntimeError('Repeated import created duplicates')
            rows = library.search()
            if not rows:
                raise RuntimeError('No real images were imported')
            for item in rows:
                if hashlib.sha256(Path(item['asset']).read_bytes()).hexdigest() != item['digest']:
                    raise RuntimeError('Copied asset hash mismatch')
            result = {'groups': len(first), 'imported': len(rows), 'repeat_added': 0,
                      'asset_hashes_verified': True}
    after = fingerprint(source / 'history')
    if before != after:
        raise RuntimeError('Source history changed during smoke test; repeat under stable conditions')
    result['source_history_unchanged'] = True
    print(json.dumps(result))


if __name__ == '__main__':
    main()
