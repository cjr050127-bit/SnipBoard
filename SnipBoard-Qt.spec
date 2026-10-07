# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
root = Path(SPECPATH)
a = Analysis(
    [str(root / 'src' / 'snipboard' / 'launcher.py')],
    pathex=[str(root / 'src')], binaries=[], datas=[],
    hiddenimports=['PySide6.QtNetwork'], hookspath=[], hooksconfig={}, runtime_hooks=[],
    excludes=['tkinter', 'PySide6.QtQml', 'PySide6.QtQuick', 'PySide6.QtTest', 'matplotlib', 'pandas', 'scipy'],
    noarchive=False, optimize=0,
)
# Qt uses Windows' unversioned ICU forwarders. Never bundle similarly named ICU
# or API-set DLLs from unrelated portable tools found on the build machine PATH.
a.binaries = [entry for entry in a.binaries
              if Path(entry[0]).name.lower() not in {'icuuc.dll', 'icuin.dll', 'icudt.dll'}
              and not Path(entry[0]).name.lower().startswith(('api-ms-win-', 'ext-ms-win-'))]
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [], exclude_binaries=True, name='SnipBoard', debug=False,
    bootloader_ignore_signals=False, strip=False, upx=False, console=False,
    icon=str(root / 'assets' / 'snipboard.png'),
    version=str(root / 'packaging' / 'version_info_qt.txt'),
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='SnipBoard-0.6.1')
