"""Repeatable synthetic metadata benchmark, not a claim about real-image decoding speed."""
import json
from pathlib import Path
import statistics
import tempfile
import time

from PIL import Image
from snipboard.catalog import Catalog
from snipboard.features import extract, VERSION


def timed(function, repeats=5):
    values = []
    for _ in range(repeats):
        start = time.perf_counter()
        result = function()
        values.append((time.perf_counter() - start) * 1000)
    return dict(median_ms=round(statistics.median(values), 2), max_ms=round(max(values), 2), rows=len(result))


def main():
    output = Path(__file__).resolve().parents[1] / 'docs' / 'quality-0.5.0'
    output.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='snipboard-benchmark-') as temporary, Catalog(Path(temporary)) as catalog:
        feature = json.dumps(extract(Image.new('RGBA', (200, 100), '#e34242')))
        digests = [f'{index:064x}' for index in range(10000)]
        with catalog.db:
            catalog.db.execute('INSERT INTO boards(id,source,group_id,name) VALUES(1,?,?,?)',
                               (catalog.local_source, 'imports', '10,000 synthetic records'))
            catalog.db.executemany('INSERT INTO items(board_id,digest,source_name) VALUES(1,?,?)',
                                  [(d, f'photo-{i}.png') for i, d in enumerate(digests)])
            catalog.db.executemany('INSERT INTO features VALUES(?,?,?,?,?)',
                                  [(d, VERSION, 200, 100, feature) for d in digests])
        group = catalog.create_work_group('10 candidates', digests[:10])
        allowed = set(digests[:10])
        report = dict(dataset='10,000 synthetic metadata records; shared solid-color feature; no asset decoding',
            full_library=timed(catalog.query),
            old_candidate_flow=timed(lambda: [r for r in catalog.query() if r['digest'] in allowed]),
            filtered_group=timed(lambda: catalog.query_filtered(work_group=group)),
            color_filter=timed(lambda: catalog.query_filtered(filters=dict(color='#e34242', coverage=.5, tolerance=20))))
        (output / 'benchmark.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
        print(json.dumps(report))


if __name__ == '__main__':
    main()
