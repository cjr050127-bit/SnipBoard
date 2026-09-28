"""Native Windows alpha rendering and interaction checks in owned hidden windows."""
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

os.environ['QT_QPA_PLATFORM'] = 'windows'
root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / 'tests'))
import test_background
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget
from snipboard.qt_board import BoardWindow
from snipboard.qt_library import LibraryWindow

def hidden_show(window):
    window.setAttribute(Qt.WA_DontShowOnScreen)
    QWidget.show(window)

output = root / 'docs' / 'background-0.5.3'
output.mkdir(exist_ok=True)
with patch.object(BoardWindow, 'show', hidden_show), patch.object(LibraryWindow, 'show', hidden_show):
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(test_background.BackgroundTests))
    sample = test_background.BackgroundTests()
    sample.setUp()
    try:
        board = sample.board()
        for mode in board.BACKGROUNDS:
            board.set_background(mode)
            test_background.test_qt.APP.processEvents()
            board.grab().save(str(output / (mode + '.png')))
        board.arm_window_tool('resize')
        board.grab().save(str(output / 'transparent-glow.png'))
    finally:
        sample.tearDown()
report = dict(passed=result.wasSuccessful(), tests=result.testsRun,
              platform=test_background.test_qt.APP.platformName(),
              dpr=test_background.test_qt.APP.primaryScreen().devicePixelRatio(),
              scope='native Qt alpha rendering, synthetic pointer events, hidden owned windows; no desktop capture or physical pointer automation')
(output / 'native-result.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
print(json.dumps(report))
raise SystemExit(0 if result.wasSuccessful() else 1)
