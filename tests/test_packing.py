import math
import random
import threading
from unittest import TestCase
from unittest.mock import patch

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from snipboard.layouts import arrange
from snipboard.packing import compact
import test_qt


def entries(ratios):
    return [dict(id=str(i), w=260., h=260/ratio, name=str(i), path='', order=i) for i,ratio in enumerate(ratios)]


class PackingTests(TestCase):
    def valid(self, rows, result, width, height, gap):
        positions = result['positions']
        self.assertIsNotNone(positions)
        self.assertEqual(set(positions), {r['id'] for r in rows})
        for row in rows:
            x,y,w,h = positions[row['id']]
            self.assertTrue(all(math.isfinite(v) for v in (x,y,w,h)))
            self.assertGreater(w,0)
            self.assertGreater(h,0)
            self.assertGreaterEqual(x,-1e-6)
            self.assertGreaterEqual(y,-1e-6)
            self.assertLessEqual(x+w,width+1e-6)
            self.assertLessEqual(y+h,height+1e-6)
            self.assertAlmostEqual(w/h,row['w']/row['h'])
        values = list(positions.values())
        for i,(x,y,w,h) in enumerate(values):
            for a,b,c,d in values[i+1:]:
                self.assertTrue(x+w+gap <= a+1e-6 or a+c+gap <= x+1e-6 or
                                y+h+gap <= b+1e-6 or b+d+gap <= y+1e-6)

    def test_mixed_ratios_improve_coverage_with_balanced_sizes(self):
        rows = entries([.4,.6,.8,1,1.5,2,3,4])
        result = compact(rows,984,684,18)
        self.valid(rows,result,984,684,18)
        old = arrange(rows,ratio=984/684,gap=18)
        scale = min(984/max(x+w for x,y,w,h in old.values()),684/max(y+h for x,y,w,h in old.values()))
        old_coverage = sum(r['w']*r['h'] for r in rows)*scale**2/(984*684)
        self.assertGreater(result['occupancy'],old_coverage+.12)
        self.assertLess(result['area_variation'],.35)

    def test_varied_windows_counts_rotations_and_extreme_ratios(self):
        rng = random.Random(520)
        for width,height in [(984,684),(500,850),(1400,400)]:
            for count in [1,2,13,35]:
                rows = entries([rng.uniform(.3,3.5) for _ in range(count)])
                for i,row in enumerate(rows):
                    angle = math.radians([0,15,90,135][i%4])
                    w,h = row['w'],row['h']
                    row['w'],row['h'] = abs(w*math.cos(angle))+abs(h*math.sin(angle)),abs(w*math.sin(angle))+abs(h*math.cos(angle))
                    row['fill'] = w*h/(row['w']*row['h'])
                self.valid(rows,compact(rows,width,height,6),width,height,6)
        rows = entries([.015,70,.5,1,2])
        self.valid(rows,compact(rows,900,600,6),900,600,6)

    def test_initial_resolution_and_previous_resize_do_not_bias_layout(self):
        # Identical shapes should receive the same layout even after individual
        # pictures were manually enlarged, or arrived at different resolutions.
        rows = entries([.4,.6,.8,1,1.5,2,3,4])
        resized = [dict(row,w=row['w']*scale,h=row['h']*scale)
                   for row,scale in zip(rows,[.05,8.,.3,50.,2.,.1,12.,.6])]
        result = compact(rows,984,684,3)
        other = compact(resized,984,684,3)
        self.valid(rows,result,984,684,3)
        self.valid(resized,other,984,684,3)
        for key,position in result['positions'].items():
            for expected,actual in zip(position,other['positions'][key]):
                self.assertAlmostEqual(expected,actual,places=5)

    def test_dense_spacing_uses_window_without_extreme_size_outliers(self):
        # A near-full window is insufficient if it leaves several tiny pictures;
        # constrain both overall variation and the most unequal pair of areas.
        samples = [([.5,.75,1,4/3,1.5,2]*4,800,600,.90),
                   ([random.Random(i).uniform(.3,3) for i in range(80)],984,684,.88)]
        for ratios,width,height,min_coverage in samples:
            with self.subTest(count=len(ratios)):
                rows = entries(ratios)
                result = compact(rows,width,height,3)
                self.valid(rows,result,width,height,3)
                areas = [w*h for x,y,w,h in result['positions'].values()]
                mean = sum(areas)/len(areas)
                variation = math.sqrt(sum((area/mean-1)**2 for area in areas)/len(areas))
                self.assertGreater(sum(areas)/(width*height),min_coverage)
                self.assertLess(variation,.35)
                self.assertLess(max(areas)/min(areas),3)

    def test_last_row_is_balanced_and_large_collections_still_fit(self):
        rows = entries([1]*13)
        result = compact(rows,984,684,18)
        self.valid(rows,result,984,684,18)
        areas = [w*h for x,y,w,h in result['positions'].values()]
        self.assertLess(max(areas)/min(areas),2)
        self.assertGreater(result['occupancy'],.85)
        rows = entries([.5,1,2,3]*60)
        result = compact(rows,1200,800,3)
        self.valid(rows,result,1200,800,3)
        self.assertLess(result['area_variation'],.35)

    def test_mixed_pair_balances_sizes_without_losing_coverage(self):
        cases = [([.6,1.6],984,684,.687,1.01),
                 ([.5,2],984,684,.635,1.01),
                 ([.6,1.6],484,834,.777,1.15),
                 ([.5,2],484,834,.719,1.49),
                 ([.3,3],984,684,.506,1.44)]
        for ratios,width,height,minimum,imbalance in cases:
            with self.subTest(ratios=ratios,window=(width,height)):
                rows = entries(ratios)
                result = compact(rows,width,height,3)
                self.valid(rows,result,width,height,3)
                areas = [w*h for x,y,w,h in result['positions'].values()]
                self.assertGreater(result['occupancy'],minimum)
                self.assertLess(max(areas)/min(areas),imbalance)

    def test_pair_preserves_spacing_rotation_bounds_and_simple_grids(self):
        for gap in (0,3,18,50):
            for width,height in ((984,684),(484,834),(1384,384)):
                with self.subTest(gap=gap,window=(width,height)):
                    rows = entries([1,1])
                    result = compact(rows,width,height,gap)
                    self.valid(rows,result,width,height,gap)
                    size = max(min((width-gap)/2,height),min(width,(height-gap)/2))
                    self.assertAlmostEqual(result['occupancy'],2*size*size/(width*height))
                    rows = entries([.4,2.5])
                    for row,angle in zip(rows,(15,45)):
                        w,h = row['w'],row['h']
                        angle = math.radians(angle)
                        row['w'] = abs(w*math.cos(angle))+abs(h*math.sin(angle))
                        row['h'] = abs(w*math.sin(angle))+abs(h*math.cos(angle))
                        row['fill'] = w*h/(row['w']*row['h'])
                    self.valid(rows,compact(rows,width,height,gap),width,height,gap)
        self.assertIsNone(compact(entries([1,2]),2,2,3)['positions'])

    def test_cancel_empty_and_invalid_dimensions(self):
        self.assertEqual(compact([],900,600)['positions'],{})
        self.assertTrue(compact(entries([1,2]),900,600,cancelled=lambda: True)['cancelled'])
        with self.assertRaises(ValueError):
            compact(entries([1]),float('nan'),600)


class PackingUiTests(TestCase):
    setUp = test_qt.QtFlowTests.setUp
    tearDown = test_qt.QtFlowTests.tearDown

    def test_fixed_window_layout_preserves_angles_fits_and_undoes(self):
        board = self.controller.show_board()
        test_qt.drain(self.controller)
        board.resize(530,760)
        for picture,angle in zip(board.pictures.values(),[0,15,90,135]):
            picture.setRotation(angle)
        board.commit()
        before = board._snapshot()
        board.arrange('optimal')
        test_qt.drain(self.controller)
        self.assertIsNotNone(board.arrange_result)
        for key,picture in board.pictures.items():
            self.assertEqual(picture.rotation(),before[key]['angle'])
            self.assertAlmostEqual(picture.rect().width()/picture.rect().height(),picture.row['width']/picture.row['height'])
            bounds = board.mapFromScene(picture.sceneBoundingRect()).boundingRect()
            self.assertTrue(board.viewport().rect().contains(bounds), str(bounds))
        board.undo(-1)
        self.assertEqual(board._snapshot(),before)

    def test_late_result_does_not_overwrite_manual_edit_or_changed_group(self):
        board = self.controller.show_board()
        test_qt.drain(self.controller)
        release = threading.Event()
        started = threading.Event()
        def slow(*args, **kwargs):
            started.set()
            release.wait(2)
            return compact(*args, **kwargs)
        with patch('snipboard.qt_board.compact', side_effect=slow):
            board.arrange('optimal')
            test_qt.APP.processEvents()
            self.assertTrue(started.wait(1))
            picture = next(iter(board.pictures.values()))
            picture.moveBy(123,45)
            board.commit()
            expected = board._snapshot()
            release.set()
            test_qt.drain(self.controller)
        self.assertEqual(board._snapshot(),expected)
        board.arrange('optimal')
        board.change_group(1)
        test_qt.drain(self.controller)
        self.assertEqual(set(board.pictures), {r['digest'] for r in board._rows(board.board_id)})
