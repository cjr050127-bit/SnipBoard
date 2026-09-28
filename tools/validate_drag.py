"""Native Windows widgets; drive only owned hidden windows and bridge the OS drag loop."""
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

os.environ['QT_QPA_PLATFORM'] = 'windows'
root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / 'tests'))
import test_drag
from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap, QColor
from snipboard.qt_library import LibraryWindow
from snipboard.qt_collections import PhotoGrid

original_show = LibraryWindow.show
def hidden_show(window):
    window.setAttribute(Qt.WA_DontShowOnScreen)
    original_show(window)

with patch.object(LibraryWindow, 'show', hidden_show):
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(test_drag.DragTests))
from snipboard import __version__
output = root / 'docs' / ('drag-' + __version__)
output.mkdir(exist_ok=True)
scale = os.environ.get('QT_SCALE_FACTOR', '1')
report = {'passed': result.wasSuccessful(), 'tests': result.testsRun,
          'platform': test_drag.test_qt.APP.platformName(), 'requested_scale': scale,
          'screen_dpr': test_drag.test_qt.APP.primaryScreen().devicePixelRatio(),
          'scope': 'actual press/move initiation and target Qt drag/drop handlers; blocking OS drag loop bridged; owned hidden windows only'}
(output / f'windows-scale-{scale}.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
print(json.dumps(report))
raise SystemExit(0 if result.wasSuccessful() else 1)
