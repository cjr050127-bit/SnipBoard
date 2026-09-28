import base64
import json
from unittest import TestCase
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDockWidget
import test_qt

class DockPanelTests(TestCase):
    setUp = test_qt.QtFlowTests.setUp
    tearDown = test_qt.QtFlowTests.tearDown

    def test_window_menu_toggles_every_panel_and_close_can_be_reopened(self):
        w = self.window
        menu = next(a.menu() for a in w.menuBar().actions() if a.text() == '窗口')
        self.assertEqual(len(w.docks), 8)
        for dock in w.docks.values():
            action = dock.toggleViewAction()
            self.assertIn(action, menu.actions())
            self.assertTrue(dock.features() & QDockWidget.DockWidgetFloatable)
            dock.close()
            self.assertTrue(dock.isHidden())
            self.assertFalse(action.isChecked())
            action.trigger()
            self.assertFalse(dock.isHidden())
            self.assertTrue(action.isChecked())

    def test_rearranged_tabbed_floating_and_hidden_state_roundtrips(self):
        w = self.window
        w.addDockWidget(Qt.RightDockWidgetArea, w.docks['aspect'])
        w.tabifyDockWidget(w.docks['labels'], w.docks['aspect'])
        w.docks['color'].setFloating(True)
        w.docks['color'].resize(330, 480)
        w.docks['dimensions'].hide()
        state = json.loads(json.dumps(w.state()))
        restored = self.controller.new_library(state)
        test_qt.drain(self.controller)
        self.assertTrue(restored.docks['color'].isFloating())
        self.assertTrue(restored.docks['dimensions'].isHidden())
        self.assertEqual(restored.dockWidgetArea(restored.docks['aspect']), Qt.RightDockWidgetArea)
        self.assertIn(restored.docks['aspect'], restored.tabifiedDockWidgets(restored.docks['labels']))
        restored.docks['color'].setFloating(False)
        restored.docks['dimensions'].show()
        self.assertTrue(w.docks['color'].isFloating())
        self.assertTrue(w.docks['dimensions'].isHidden())

    def test_hiding_filters_keeps_conditions_and_work_collection(self):
        w = self.window
        w.filter_panel.orientation.setCurrentIndex(2)
        w.filter_panel.color_enabled.setChecked(True)
        group = self.controller.catalog.create_work_group('Current')
        self.controller.catalog.set_current_work_group(group)
        expected = w.filter_panel.state()
        for dock in w.docks.values():
            dock.hide()
        self.assertEqual(w.filter_panel.state(), expected)
        self.assertEqual(self.controller.catalog.get_setting('work.current'), group)
        w.toggle_filter_panels()
        self.assertTrue(all(not w.docks[k].isHidden() for k in w.filter_panel.sections))
        self.assertEqual(w.filter_panel.state(), expected)

    def test_reset_restores_docking_without_resetting_content(self):
        w = self.window
        w.search.setText('image')
        w.filter_panel.dimensions['min_width'].setValue(190)
        w.docks['labels'].setFloating(True)
        w.docks['color'].hide()
        w.reset_panel_layout()
        self.assertFalse(w.docks['labels'].isFloating())
        self.assertFalse(w.docks['color'].isHidden())
        self.assertEqual(w.search.text(), 'image')
        self.assertEqual(w.filter_panel.state()['min_width'], 190)

    def test_old_or_invalid_panel_state_keeps_window_usable(self):
        for encoded in (None, 'not base64!', base64.b64encode(b'broken state').decode()):
            w = self.controller.new_library({'search': 'image', 'panel_layout': encoded})
            test_qt.drain(self.controller)
            self.assertEqual(w.search.text(), 'image')
            self.assertTrue(all(not d.isFloating() for d in w.docks.values()))
            self.assertFalse(w.docks['color'].isHidden())

    def test_recommendations_reflow_when_only_floating_panel_resizes(self):
        w = self.window
        w.show_photo(w.rows[0])
        test_qt.drain(self.controller)
        dock = w.docks['recommendations']
        dock.setFloating(True)
        dock.resize(330, 280)
        test_qt.APP.processEvents()
        test_qt.APP.processEvents()
        narrow = len(w.rec_models[0].rows)
        dock.resize(950, 280)
        test_qt.APP.processEvents()
        test_qt.APP.processEvents()
        self.assertGreater(len(w.rec_models[0].rows), narrow)
