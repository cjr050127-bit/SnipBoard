import tempfile
import unittest
from pathlib import Path

from PIL import Image

from snipboard.catalog import Catalog


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.catalog = Catalog(self.root / 'library')
        self.addCleanup(self.catalog.close)

    def test_import_persists_with_filters_tags_groups_and_deduplicates_source(self):
        source = self.root / 'source'
        folder = source / 'history' / 'ABC123'
        folder.mkdir(parents=True)
        picture = folder / 'red.png'
        Image.new('RGB', (160, 90), '#e34242').save(picture)
        original = picture.read_bytes()
        self.catalog.sync_source(source)
        current = self.catalog.create_work_group('工作中')
        self.catalog.set_current_work_group(current)
        result = self.catalog.import_images([picture, picture])
        self.assertEqual((result['added'], result['duplicates'], result['errors']), (1, 1, []))
        board = result['board_id']
        rows = self.catalog.query_filtered([board], filters=dict(orientation='landscape', color='#e34242', coverage=.8))
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(self.catalog.query()), 1)
        self.assertEqual(len(rows[0]['memberships']), 2)
        digest = rows[0]['digest']
        self.assertEqual(self.catalog.collection_query(f'work:{current}'), [])
        self.catalog.set_tags(digest, ['本地参考'])
        self.catalog.add_to_work_group(current, [digest])
        self.assertEqual(len(self.catalog.collection_query(f'work:{current}')), 1)
        self.assertEqual(picture.read_bytes(), original)
        picture.unlink()
        self.catalog.sync_source(source)
        with Catalog(self.catalog.root) as reopened:
            self.assertEqual(len(reopened.query_filtered([board])), 1)
            self.assertEqual(reopened.tags_for(digest), ['本地参考'])
            label = next(g for g in reopened.label_rows() if g['id'] == board)
            self.assertEqual((label['kind'], label['count']), ('local', 1))
            self.assertTrue(Path(reopened.query()[0]['asset']).exists())

    def test_formats_orientation_and_partial_failure_preserve_originals(self):
        paths = []
        for extension, color in [('jpg', 'red'), ('webp', 'green'), ('bmp', 'blue'), ('tiff', 'yellow'), ('gif', 'purple')]:
            path = self.root / f'photo.{extension}'
            image = Image.new('RGB', (120, 60), color)
            options = {}
            if extension == 'jpg':
                exif = Image.Exif()
                exif[274] = 6
                options['exif'] = exif
            image.save(path, **options)
            paths.append(path)
        before = {p: p.read_bytes() for p in paths}
        corrupt = self.root / 'broken.png'
        corrupt.write_bytes(b'not an image')
        result = self.catalog.import_images([corrupt, *paths, self.root / 'missing.png'])
        self.assertEqual((result['added'], len(result['errors'])), (5, 2))
        rows = self.catalog.query()
        jpeg = next(r for r in rows if r['source_name'] == 'photo.jpg')
        self.assertEqual((jpeg['width'], jpeg['height']), (60, 120))
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content)
        for row in rows:
            with Image.open(row['asset']) as asset:
                self.assertEqual(asset.format, 'PNG')
        self.assertEqual(self.catalog.query(archived=True), [])
        again = self.catalog.import_images(paths)
        self.assertEqual((again['added'], again['duplicates']), (0, 5))

    def test_cancelled_import_does_not_create_empty_collection(self):
        result = self.catalog.import_images([self.root / 'unused.png'], cancelled=lambda: True)
        self.assertTrue(result['cancelled'])
        self.assertIsNone(result['board_id'])
        self.assertEqual(self.catalog.group_rows(), [])
