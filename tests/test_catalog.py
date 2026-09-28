from pathlib import Path
import hashlib
import tempfile
import unittest
from PIL import Image

from snipboard.catalog import Catalog
from snipboard.library import Library
from snipboard.features import extract, color_score
from snipboard.layouts import arrange


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.source = self.root / 'source'
        self.group = self.source / 'history' / 'ABC123'
        self.group.mkdir(parents=True)
        self.image = self.group / 'red.png'
        Image.new('RGB', (160, 100), '#e34242').save(self.image)
        self.catalog = Catalog(self.root / 'library')
        self.addCleanup(self.catalog.close)

    def test_only_paste_groups_not_screenshot_history(self):
        snip = self.source / 'history' / 'snip'
        snip.mkdir()
        (snip / 'record.sp1').write_bytes(b'not a supported or inspected file')
        Image.new('RGB', (50, 50), 'blue').save(snip / 'history.png')
        before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.source.rglob('*') if p.is_file()}
        report = self.catalog.sync_source(self.source)
        self.assertEqual(report['files'], 1)
        self.assertEqual(report['errors'], [])
        self.assertEqual(len(self.catalog.query()), 1)
        self.assertEqual(len(self.catalog.group_rows()), 1)
        self.assertFalse(any('snip' in r['source_name'] for r in self.catalog.query()))
        after = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.source.rglob('*') if p.is_file()}
        self.assertEqual(before, after)

    def test_moving_image_updates_membership_but_keeps_asset(self):
        self.catalog.sync_source(self.source)
        old = self.catalog.group_rows()[0]['id']
        target = self.source / 'history' / 'DEF456'
        target.mkdir()
        self.image.rename(target / self.image.name)
        self.catalog.sync_source(self.source)
        self.assertEqual(self.catalog.query([old]), [])
        self.assertEqual(len(self.catalog.query([old], archived=True)), 1)
        self.assertEqual(len(self.catalog.query()), 1)
        self.assertEqual(len(list((self.catalog.root / 'assets').glob('*.png'))), 1)

    def test_shared_asset_deduplicates_results_and_retains_groups(self):
        target = self.source / 'history' / 'DEF456'
        target.mkdir()
        (target / 'same.png').write_bytes(self.image.read_bytes())
        self.catalog.sync_source(self.source)
        rows = self.catalog.query()
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(rows[0]['memberships']), 2)
        self.assertEqual(self.catalog.query([]), [])
        self.assertEqual(self.catalog.sync_source(self.source)['added'], 0)

    def test_replaced_file_archives_previous_content(self):
        self.catalog.sync_source(self.source)
        original = self.catalog.query()[0]['digest']
        Image.new('RGB', (160, 100), 'blue').save(self.image)
        self.catalog.sync_source(self.source)
        self.assertNotEqual(self.catalog.query()[0]['digest'], original)
        self.assertEqual(self.catalog.query(archived=True)[0]['digest'], original)

    def test_group_color_filter_and_recommendations(self):
        Image.new('RGB', (120, 100), '#ea4a41').save(self.group / 'similar.png')
        other = self.source / 'history' / 'DEF456'
        other.mkdir()
        Image.new('RGB', (120, 100), '#4385db').save(other / 'blue.png')
        self.catalog.sync_source(self.source)
        rows = self.catalog.query()
        red = self.catalog.filter_colors(rows, ['#e34242'])
        self.assertEqual(len(red), 2)
        recommended = self.catalog.recommend(red[0], red)
        self.assertEqual(len(recommended), 1)
        self.assertNotEqual(recommended[0]['digest'], red[0]['digest'])
        self.assertNotEqual(recommended[0]['source_name'], 'blue.png')

    def test_migration_backs_up_and_keeps_annotations(self):
        root = self.root / 'old'
        with Library(root) as old:
            old.collect(self.source, 'ABC123')
            old.annotate(old.search()[0]['id'], 'keep me', ['red'])
            old.save_positions(old.boards()[0]['id'], {old.search()[0]['id']: (7., 8., 200.)})
        with Catalog(root) as new:
            self.assertEqual(new.search()[0]['note'], 'keep me')
            self.assertEqual(new.items(new.boards()[0]['id'])[0]['x'], 7.)
            self.assertEqual(new.db.execute('PRAGMA user_version').fetchone()[0], Catalog.schema_version)
        self.assertEqual(len(list((root / 'backups').glob('*.sqlite3'))), 1)

    def test_transparency_is_not_black_and_alignment_keeps_aspect(self):
        image = Image.new('RGBA', (100, 100), (0, 0, 0, 0))
        self.assertEqual(color_score(extract(image), '#151515'), 0)
        image.paste((227, 66, 66, 255), (0, 0, 50, 50))
        self.assertGreater(color_score(extract(image), '#e34242'), .9)
        entries = [{'id': i, 'name': str(i), 'path': str(i), 'order': i, 'w': 80 + i * 10, 'h': 40 + i * 15}
                   for i in range(10)]
        for mode in ('optimal', 'name', 'addition', 'path', 'random', 'order'):
            positions = arrange(entries, mode)
            self.assertEqual(len(positions), 10)
            for i, (_, _, w, h) in positions.items():
                self.assertEqual((w, h), (entries[i]['w'], entries[i]['h']))
            rects = list(positions.values())
            for i, (x, y, w, h) in enumerate(rects):
                for xx, yy, ww, hh in rects[i + 1:]:
                    self.assertFalse(x < xx + ww and xx < x + w and y < yy + hh and yy < y + h)
