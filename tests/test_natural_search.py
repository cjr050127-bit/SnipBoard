"""Behavioral tests use an isolated catalog, never the user's image library."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from snipboard.catalog import Catalog
from snipboard.natural_search import search_catalog
from snipboard.query_plan import Condition, METADATA_WARNING, QueryPlan, parse_query


class NaturalSearchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.catalog = Catalog(self.root / 'catalog')
        self.addCleanup(self.catalog.close)
        examples = [('red-side.png', (160, 90), '#e34242', ['人物', '插画', '侧脸'], '逆光 红色衣服'),
                    ('blue-photo.png', (90, 160), '#4385db', ['人物', '照片'], '自然光'),
                    ('red-tutorial.png', (100, 100), '#e34242', ['教程', '插画'], '正方形示例'),
                    ('black-side.png', (160, 90), '#151515', ['人物', '插画', '侧脸'], '暗处')]
        for name, size, color, tags, note in examples:
            path = self.root / name
            Image.new('RGB', size, color).save(path)
            self.catalog.import_images([path])
            row = next(r for r in self.catalog.query() if r['source_name'] == name)
            self.catalog.annotate(row['id'], note, tags)
        self.rows = {r['source_name']: r for r in self.catalog.query()}
        self.group = self.catalog.create_work_group('人物参考', [self.rows['red-side.png']['digest'],
                                                               self.rows['blue-photo.png']['digest']])
        self.catalog.set_current_work_group(self.group)

    def plan(self, text, previous=None):
        return parse_query(text, self.catalog.label_rows(), self.catalog.work_groups(), previous)

    def names(self, plan, **kwargs):
        return {r['source_name'] for r in search_catalog(self.catalog, plan, **kwargs).rows}

    def test_named_group_and_exact_filters_are_conjunctive(self):
        plan = self.plan('在人物参考工作组里找横向的侧脸插画，宽度至少160像素，比例16:9')
        self.assertEqual(self.names(plan), {'red-side.png'})
        self.assertEqual({c.kind for c in plan.conditions}, {'work', 'orientation', 'label', 'dimension', 'ratio'})

    def test_current_work_group(self):
        self.assertEqual(self.names(self.plan('当前工作组里的图片')), {'red-side.png', 'blue-photo.png'})

    def test_labels_and_exclusion(self):
        self.assertEqual(self.names(self.plan('人物和插画，不要照片')), {'red-side.png', 'black-side.png'})

    def test_or_is_not_applied_to_later_orientation(self):
        self.assertEqual(self.names(self.plan('插画或照片的横图')), {'red-side.png', 'black-side.png'})

    def test_exclude_or_means_neither_label(self):
        self.assertEqual(self.names(self.plan('全部图片，不要照片或教程')), {'red-side.png', 'black-side.png'})

    def test_follow_up_narrows_instead_of_replacing(self):
        first = self.plan('人物和插画')
        second = self.plan('再只留红色横图', first)
        self.assertEqual(self.names(second), {'red-side.png'})
        self.assertEqual(second.history, ('人物和插画', '再只留红色横图'))

    def test_restart_replaces_previous_scope(self):
        previous = self.plan('在人物参考组里找横图')
        self.assertEqual(self.names(self.plan('重新找正方形图片', previous)), {'red-tutorial.png'})

    def test_property_replacement_is_explicit(self):
        previous = self.plan('人物横图')
        self.assertEqual(self.names(self.plan('改成竖图', previous)), {'blue-photo.png'})

    def test_keyword_uses_metadata_and_explains_limit(self):
        plan = self.plan('找逆光')
        self.assertEqual(self.names(plan), {'red-side.png'})
        self.assertIn(METADATA_WARNING, plan.warnings)
        self.assertEqual([c.kind for c in plan.conditions], ['keyword'])

    def test_unknown_descriptions_are_not_silently_rewritten(self):
        plan = self.plan('找中世纪构图')
        self.assertEqual(plan.conditions[0].value, '中世纪构图')

    def test_object_color_does_not_become_whole_image_color(self):
        plan = self.plan('找红色衣服')
        self.assertEqual([c.kind for c in plan.conditions], ['keyword'])
        self.assertEqual(self.names(plan), {'red-side.png'})

    def test_whole_image_color_and_brightness(self):
        self.assertEqual(self.names(self.plan('红色横图')), {'red-side.png'})
        self.assertEqual(self.names(self.plan('横图，亮度小于40')), {'black-side.png'})

    def test_dimension_inequality_is_strict(self):
        self.assertEqual(self.names(self.plan('宽度大于160')), set())
        self.assertEqual(self.names(self.plan('宽度大于等于160且高度小于100')), {'red-side.png', 'black-side.png'})

    def test_keywords_are_case_insensitive_and_not_visual_inference(self):
        self.assertEqual(self.names(self.plan('关键词：RED-SIDE')), {'red-side.png'})
        self.assertEqual(self.names(self.plan('找一个独角兽')), set())

    def test_quoted_keyword_preserves_spaces(self):
        plan = self.plan('关键词：“逆光 红色衣服”')
        self.assertEqual(self.names(plan), {'red-side.png'})
        self.assertEqual(plan.conditions[0].value, '逆光 红色衣服')

    def test_empty_query_and_empty_scope_never_expand_to_all(self):
        self.assertEqual(self.names(self.plan('')), set())
        self.assertEqual(self.names(self.plan('全部图片'), scope_digests=()), set())

    def test_scope_snapshot_is_strict(self):
        scope = (self.rows['red-tutorial.png']['digest'],)
        self.assertEqual(self.names(self.plan('插画'), scope_digests=scope), {'red-tutorial.png'})

    def test_invalid_ratio_does_not_divide_by_zero_or_broaden_results(self):
        plan = self.plan('比例16:0')
        self.assertEqual(self.names(plan), set())
        self.assertTrue(plan.warnings)

    def test_missing_current_group_does_not_broaden_results(self):
        plan = parse_query('当前工作组', groups=[])
        self.assertEqual(self.names(plan), set())
        self.assertIn('尚未设置当前工作组', plan.warnings[0])

    def test_remove_chip_changes_query_and_removes_stale_keyword_warning(self):
        plan = self.plan('横图，逆光')
        keyword = next(c for c in plan.conditions if c.kind == 'keyword')
        changed = plan.without(keyword.id)
        self.assertEqual(self.names(changed), {'red-side.png', 'black-side.png'})
        self.assertNotIn(METADATA_WARNING, changed.warnings)

    def test_limit_reports_full_matching_count(self):
        result = search_catalog(self.catalog, self.plan('全部图片'), limit=2)
        self.assertEqual(len(result.rows), 2)
        self.assertEqual(result.matched_count, 4)
        self.assertTrue(result.truncated)

    def test_search_does_not_open_images_or_rebuild_features(self):
        with patch.object(self.catalog, 'ensure_feature', side_effect=AssertionError('analysis forbidden')), \
                patch('PIL.Image.open', side_effect=AssertionError('image I/O forbidden')):
            self.assertEqual(self.names(self.plan('红色横图')), {'red-side.png'})

    def test_cancelled_search_leaves_catalog_unchanged_and_connection_usable(self):
        before = list(self.catalog.db.iterdump())
        counter = 0
        def cancelled():
            nonlocal counter
            counter += 1
            return counter > 4
        result = search_catalog(self.catalog, self.plan('全部图片'), cancel=cancelled)
        self.assertTrue(result.cancelled)
        self.assertEqual(result.rows, [])
        self.assertEqual(before, list(self.catalog.db.iterdump()))
        self.assertEqual(len(self.catalog.query()), 4)

    def test_missing_feature_is_not_evidence_for_excluded_color_or_brightness(self):
        self.catalog.db.execute('DELETE FROM features WHERE digest=?', (self.rows['red-side.png']['digest'],))
        self.catalog.db.commit()
        self.assertNotIn('red-side.png', self.names(self.plan('全部图片，不要红色')))
        self.assertNotIn('red-side.png', self.names(self.plan('全部图片，不要亮度大于40')))
        self.assertNotIn('red-side.png', self.names(self.plan('方图')))

    def test_negative_label_does_not_include_archived_images(self):
        source = self.root / 'source'
        group = source / 'history' / 'ABC123'
        group.mkdir(parents=True)
        image = group / 'archived.png'
        Image.new('RGB', (70, 40), 'green').save(image)
        self.catalog.sync_source(source)
        archived = next(r for r in self.catalog.query() if r['source_name'] == 'archived.png')
        self.catalog.set_tags(archived['digest'], ['插画'])
        image.unlink()
        self.catalog.sync_source(source)
        self.assertNotIn('archived.png', self.names(self.plan('全部图片，不要照片')))
        self.assertIn('archived.png', self.names(self.plan('插画')))

    def test_narrow_properties_are_applied_before_catalog_hydration(self):
        original = self.catalog.query
        captured = []
        def query(**kwargs):
            captured.append(kwargs)
            return original(**kwargs)
        with patch.object(self.catalog, 'query', side_effect=query):
            self.assertEqual(self.names(self.plan('横图，宽至少160')), {'red-side.png', 'black-side.png'})
        self.assertEqual(captured[0]['digests'], {self.rows['red-side.png']['digest'], self.rows['black-side.png']['digest']})

    def test_explicit_work_condition_from_ui(self):
        condition = Condition('scope-work', 'work', self.group, '工作组：人物参考')
        plan = QueryPlan(conditions=(condition,))
        self.assertEqual(self.names(plan), {'red-side.png', 'blue-photo.png'})


if __name__ == '__main__':
    unittest.main()
