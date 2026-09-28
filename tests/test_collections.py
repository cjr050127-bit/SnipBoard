import json
import unittest
from PIL import Image
import test_catalog
from snipboard.catalog import Catalog
from snipboard.features import extract


class CollectionTests(unittest.TestCase):
    setUp = test_catalog.CatalogTests.setUp

    def test_source_label_additions_survive_sync_reopen_removal_and_backup(self):
        import hashlib
        from snipboard.maintenance import restore_backup
        other = self.source / 'history' / 'DEF456'
        other.mkdir()
        image = other / 'blue.png'
        Image.new('RGB', (90, 170), '#4385db').save(image)
        original = hashlib.sha256(image.read_bytes()).hexdigest()
        self.catalog.sync_source(self.source)
        reference = next(g['id'] for g in self.catalog.group_rows() if g['group_id'] == 'ABC123')
        digest = next(r['digest'] for r in self.catalog.query() if r['width'] == 90)
        self.catalog.set_tags(digest, ['手动'])
        manual = next(g['id'] for g in self.catalog.label_rows() if g['name'] == '手动')
        self.assertEqual(self.catalog.add_to_label(reference, [digest, digest, '0' * 64]), 1)
        self.assertEqual(self.catalog.add_to_label(reference, [digest]), 0)
        self.assertEqual(hashlib.sha256(image.read_bytes()).hexdigest(), original)
        # Simulate a later source removal: the explicit local label keeps its image.
        image.unlink()
        self.catalog.sync_source(self.source)
        self.assertEqual([r['digest'] for r in self.catalog.query_filtered([reference, manual], filters={'orientation': 'portrait'})], [digest])
        name = next(g['name'] for g in self.catalog.label_rows() if g['id'] == reference)
        self.assertIn(digest, {r['digest'] for r in self.catalog.query_filtered([reference], text=name)})
        self.catalog.remove_label(reference)
        self.assertEqual(self.catalog.collection_query(reference), [])
        self.catalog.restore_labels([reference])
        with Catalog(self.catalog.root) as reopened:
            self.assertIn(digest, {r['digest'] for r in reopened.collection_query(reference)})
            reopened.delete_images([digest])
            self.assertNotIn(digest, {r['digest'] for r in reopened.collection_query(reference)})
            reopened.restore_images([digest])
        archive = self.catalog.root.parent / 'labels.zip'
        self.catalog.backup(archive)
        destination = self.catalog.root.parent / 'restored-labels'
        restore_backup(archive, destination, Catalog.schema_version)
        with Catalog(destination) as restored:
            self.assertIn(digest, {r['digest'] for r in restored.collection_query(reference)})

    def test_shared_manual_labels_intersect_source_and_work_group(self):
        other = self.source / 'history' / 'DEF456'
        other.mkdir()
        (other / 'copy.png').write_bytes(self.image.read_bytes())
        Image.new('RGB', (90, 170), '#4385db').save(other / 'blue.png')
        self.catalog.sync_source(self.source)
        row = next(r for r in self.catalog.query() if r['width'] == 160)
        self.catalog.set_tags(row['digest'], ['角色', '暖光', '角色'])
        labels = {r['name']: r['id'] for r in self.catalog.label_rows() if r['kind'] == 'manual'}
        group = self.catalog.create_work_group('作品 A', [row['digest']])
        source_id = self.catalog.group_rows()[0]['id']
        filters = dict(orientation='landscape', min_width=150, color='#e34242', coverage=.7,
                       tolerance=15, brightness_min=30, brightness_max=70)
        result = self.catalog.query_filtered([source_id, labels['角色'], labels['暖光']], group, filters=filters)
        self.assertEqual([r['digest'] for r in result], [row['digest']])
        self.assertEqual(len(result[0]['memberships']), 2)
        self.assertEqual(self.catalog.query_filtered([labels['角色']], group, filters=dict(filters, orientation='portrait')), [])
        self.catalog.set_tags(row['digest'], ['角色'])
        self.assertEqual(self.catalog.query_filtered([labels['角色'], labels['暖光']]), [])
        self.assertEqual(len(self.catalog.query_filtered([labels['角色'], labels['暖光']], match_all=False)), 1)
        self.assertEqual(len(self.catalog.query(text='角色')), 1)

    def test_first_scan_is_baseline_then_new_content_captured_once(self):
        group = self.catalog.create_work_group('工作中')
        self.catalog.set_current_work_group(group)
        self.assertEqual(self.catalog.sync_source(self.source)['auto_added'], 0)
        self.assertEqual(self.catalog.collection_query(f'work:{group}'), [])
        Image.new('RGB', (70, 120), 'blue').save(self.group / 'new.png')
        self.assertEqual(self.catalog.sync_source(self.source)['auto_added'], 1)
        self.assertEqual(self.catalog.sync_source(self.source)['auto_added'], 0)
        digest = self.catalog.collection_query(f'work:{group}')[0]['digest']
        self.catalog.remove_from_work_group(group, [digest])
        self.assertEqual(self.catalog.sync_source(self.source)['auto_added'], 0)
        second = self.catalog.create_work_group('下一作品')
        self.catalog.set_current_work_group(second)
        Image.new('RGB', (70, 120), 'green').save(self.group / 'new.png')
        self.assertEqual(self.catalog.sync_source(self.source)['auto_added'], 1)
        self.assertEqual(len(self.catalog.collection_query(f'work:{second}')), 1)
        self.assertEqual(self.catalog.collection_query(f'work:{group}'), [])
        self.catalog.set_current_work_group(None)
        Image.new('RGB', (70, 120), 'orange').save(self.group / 'later.png')
        self.assertEqual(self.catalog.sync_source(self.source)['auto_added'], 0)

    def test_mid_scan_work_group_change_does_not_route_into_wrong_group(self):
        self.catalog.sync_source(self.source)
        first = self.catalog.create_work_group('A')
        second = self.catalog.create_work_group('B')
        self.catalog.set_current_work_group(first)
        Image.new('RGB', (80, 150), 'blue').save(self.group / 'z-new.png')
        def progress(count, name):
            if count == 1:
                with Catalog(self.catalog.root) as other:
                    other.set_current_work_group(second)
        report = self.catalog.sync_source(self.source, progress=progress)
        self.assertEqual(report['errors'], [])
        self.assertEqual(report['auto_added'], 1)
        self.assertEqual(self.catalog.collection_query(f'work:{first}'), [])
        self.assertEqual(len(self.catalog.collection_query(f'work:{second}')), 1)

    def test_work_group_and_manual_labels_keep_local_copy_and_layout(self):
        self.catalog.sync_source(self.source)
        row = self.catalog.query()[0]
        self.catalog.set_tags(row['digest'], ['保留'])
        tag = next(r['id'] for r in self.catalog.label_rows() if r['kind'] == 'manual')
        group = self.catalog.create_work_group('参考', [row['digest'], row['digest'], 'bad'])
        self.assertEqual(self.catalog.work_groups()[0]['count'], 1)
        self.catalog.save_layout(f'work:{group}', {'items': {}, 'viewport': [1, 2, 3]})
        self.image.unlink()
        self.catalog.sync_source(self.source)
        self.assertEqual(self.catalog.query(), [])
        self.assertEqual(len(self.catalog.collection_query(tag)), 1)
        self.assertEqual(len(self.catalog.collection_query(f'work:{group}')), 1)
        with Catalog(self.catalog.root) as reopened:
            self.assertEqual(reopened.layout(f'work:{group}')['viewport'], [1, 2, 3])
            self.assertEqual(reopened.tags_for(row['digest']), ['保留'])

    def test_color_coverage_lightness_ratio_and_transparency(self):
        image = Image.new('RGBA', (100, 100), (0, 0, 255, 255))
        image.paste((255, 0, 0, 255), (0, 0, 25, 100))
        row = dict(width=100, height=100, feature=extract(image))
        self.assertEqual(len(Catalog.filter_properties([row], dict(color='#ff0000', tolerance=8, coverage=.20))), 1)
        self.assertEqual(Catalog.filter_properties([row], dict(color='#ff0000', tolerance=8, coverage=.35)), [])
        self.assertEqual(len(Catalog.filter_properties([row], dict(orientation='square', ratio=1, min_width=99, max_width=101))), 1)
        self.assertEqual(Catalog.filter_properties([row], dict(ratio=16/9)), [])
        self.assertEqual(Catalog.filter_properties([row], dict(brightness_min=90)), [])
        clear = dict(width=100, height=100, feature=extract(Image.new('RGBA', (10, 10))))
        self.assertEqual(Catalog.filter_properties([clear], dict(color='#000000')), [])
        self.assertEqual(Catalog.filter_properties([clear], dict(brightness_max=20)), [])

    def test_deleting_work_group_preserves_images_tags_and_clears_active_state(self):
        self.catalog.sync_source(self.source)
        row = self.catalog.query()[0]
        self.catalog.set_tags(row['digest'], ['保留标签'])
        group = self.catalog.create_work_group('删除此组', [row['digest']])
        self.catalog.set_current_work_group(group)
        self.catalog.set_setting('board.last_group', f'work:{group}')
        self.catalog.set_setting('windows', [{'work_group': group, 'search': '保持筛选'}])
        self.catalog.save_layout(f'work:{group}', {'items': {}})
        self.assertTrue(self.catalog.delete_work_group(group))
        self.assertFalse(self.catalog.delete_work_group(group))
        self.assertEqual(self.catalog.work_groups(), [])
        self.assertEqual(self.catalog.db.execute('SELECT count(*) FROM work_members').fetchone()[0], 0)
        self.assertIsNone(self.catalog.get_setting('work.current'))
        self.assertIsNone(self.catalog.get_setting('board.last_group'))
        self.assertEqual(self.catalog.get_setting('windows'), [{'work_group': None, 'search': '保持筛选'}])
        self.assertIsNone(self.catalog.layout(f'work:{group}'))
        self.catalog.save_layout(f'work:{group}', {'items': {'stale': {}}})
        self.assertIsNone(self.catalog.layout(f'work:{group}'))
        self.assertEqual(self.catalog.tags_for(row['digest']), ['保留标签'])
        self.assertEqual(len(self.catalog.query()), 1)
        self.assertTrue(self.image.exists())
        self.assertTrue((self.catalog.root / 'assets' / f"{row['digest']}.png").exists())
        Image.new('RGB', (80, 100), 'green').save(self.group / 'new.png')
        self.assertEqual(self.catalog.sync_source(self.source)['auto_added'], 0)
        replacement = self.catalog.create_work_group('新参考组')
        self.assertGreater(replacement, group)
        other = self.catalog.create_work_group('继续工作')
        self.catalog.set_current_work_group(other)
        self.catalog.delete_work_group(replacement)
        self.assertEqual(self.catalog.get_setting('work.current'), other)

    def test_schema_two_migration_unions_legacy_tags_and_preserves_layout(self):
        self.catalog.sync_source(self.source)
        digest = self.catalog.query()[0]['digest']
        with self.catalog.db:
            self.catalog.db.execute('UPDATE items SET tags=?', (json.dumps(['旧标签']),))
            self.catalog.db.execute('PRAGMA user_version=2')
        self.catalog.save_layout(self.catalog.group_rows()[0]['id'], {'items': {'test': {'angle': 90}}})
        with Catalog(self.catalog.root) as reopened:
            self.assertEqual(reopened.tags_for(digest), ['旧标签'])
            self.assertEqual(reopened.db.execute('PRAGMA user_version').fetchone()[0], Catalog.schema_version)
            self.assertTrue(reopened.get_setting('monitor.ready:' + str(self.source.resolve())))
        self.assertTrue(list((self.catalog.root / 'backups').glob(f'before-v{Catalog.schema_version}-*.sqlite3')))

    def test_schema_four_upgrade_preserves_labels_and_adds_drop_membership(self):
        self.catalog.sync_source(self.source)
        digest = self.catalog.query()[0]['digest']
        self.catalog.set_tags(digest, ['已有标签'])
        reference = self.catalog.group_rows()[0]['id']
        with self.catalog.db:
            self.catalog.db.execute('DROP TABLE label_members')
            self.catalog.db.execute('PRAGMA user_version=4')
        with Catalog(self.catalog.root) as reopened:
            self.assertEqual(reopened.tags_for(digest), ['已有标签'])
            self.assertEqual(reopened.add_to_label(reference, [digest]), 0)
            self.assertEqual(reopened.db.execute('SELECT count(*) FROM label_members').fetchone()[0], 1)
        self.assertTrue(list((self.catalog.root / 'backups').glob('before-v5-*.sqlite3')))


if __name__ == '__main__':
    unittest.main()
