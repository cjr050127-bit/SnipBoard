import json
import unittest

import test_catalog
from snipboard.catalog import Catalog


class RemovalTests(unittest.TestCase):
    setUp = test_catalog.CatalogTests.setUp

    def test_remove_automatic_label_keeps_images_and_survives_sync_and_reopen(self):
        self.catalog.sync_source(self.source)
        label = self.catalog.label_rows()[0]['id']
        self.catalog.db.execute('UPDATE boards SET name=? WHERE id=?', ('专属来源标签', label))
        self.catalog.db.commit()
        digest = self.catalog.query()[0]['digest']
        self.catalog.set_tags(digest, ['保留'])
        self.assertTrue(self.catalog.remove_label(label))
        self.assertFalse(self.catalog.remove_label(label))
        self.assertEqual(self.catalog.query(text='专属来源标签'), [])
        self.assertEqual(self.catalog.query()[0]['memberships'], [])
        self.assertEqual(self.catalog.query()[0]['board_name'], '未分类')
        self.assertEqual(self.catalog.query_filtered([label]), [])
        self.assertEqual(self.catalog.query([label]), [])
        self.catalog.sync_source(self.source)
        with Catalog(self.catalog.root) as reopened:
            self.assertEqual(len(reopened.query()), 1)
            self.assertEqual(reopened.tags_for(digest), ['保留'])
            self.assertNotIn(label, [g['id'] for g in reopened.label_rows()])

    def test_remove_manual_label_clears_memberships_without_removing_other_tags(self):
        self.catalog.sync_source(self.source)
        digest = self.catalog.query()[0]['digest']
        self.catalog.set_tags(digest, ['移除', '保留'])
        label = next(g['id'] for g in self.catalog.label_rows() if g['name'] == '移除')
        self.assertTrue(self.catalog.remove_label(label))
        self.assertEqual(self.catalog.tags_for(digest), ['保留'])
        self.assertEqual(json.loads(self.catalog.db.execute('SELECT tags FROM items').fetchone()[0]), ['保留'])
        self.assertNotIn(label, [g['id'] for g in self.catalog.label_rows()])
        self.assertEqual(self.catalog.query(text='移除'), [])
        self.catalog.set_tags(digest, ['移除', '保留'])
        self.assertIn(label, [g['id'] for g in self.catalog.label_rows()])

    def test_delete_image_hides_everywhere_and_sync_cannot_auto_collect_it_again(self):
        self.catalog.sync_source(self.source)
        digest = self.catalog.query()[0]['digest']
        before = self.image.read_bytes()
        self.catalog.set_tags(digest, ['人物'])
        work = self.catalog.create_work_group('参考', [digest])
        self.catalog.set_current_work_group(work)
        self.assertEqual(self.catalog.delete_images([digest, digest, 'invalid']), 1)
        self.assertEqual(self.catalog.delete_images([digest]), 0)
        self.assertEqual(self.catalog.query(), [])
        self.assertEqual(self.catalog.query(archived=None), [])
        self.assertEqual(self.catalog.collection_query(f'work:{work}'), [])
        self.assertTrue(all(g['count'] == 0 for g in self.catalog.label_rows()))
        self.assertEqual(self.catalog.work_groups()[0]['count'], 0)
        (self.group / 'copy.png').write_bytes(before)
        self.assertEqual(self.catalog.sync_source(self.source)['auto_added'], 0)
        with Catalog(self.catalog.root) as reopened:
            self.assertEqual(reopened.query(), [])
            self.assertFalse(reopened.has_image(digest))
        self.assertEqual(self.image.read_bytes(), before)
        self.assertTrue((self.catalog.root / 'assets' / f'{digest}.png').exists())
        restored = self.catalog.import_images([self.image])
        self.assertEqual(restored['added'], 1)
        self.assertTrue(self.catalog.has_image(digest))

    def test_schema_three_upgrade_backs_up_and_preserves_existing_data(self):
        self.catalog.sync_source(self.source)
        digest = self.catalog.query()[0]['digest']
        self.catalog.set_tags(digest, ['保留'])
        with self.catalog.db:
            self.catalog.db.execute('DROP TABLE removed_labels')
            self.catalog.db.execute('DROP TABLE deleted_images')
            self.catalog.db.execute('PRAGMA user_version=3')
        with Catalog(self.catalog.root) as reopened:
            self.assertEqual(reopened.db.execute('PRAGMA user_version').fetchone()[0], Catalog.schema_version)
            self.assertTrue(reopened.has_image(digest))
            self.assertEqual(reopened.tags_for(digest), ['保留'])
        self.assertTrue(list((self.catalog.root / 'backups').glob(f'before-v{Catalog.schema_version}-*.sqlite3')))
