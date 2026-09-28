# Third-party software / 第三方软件

SnipBoard 自有代码使用 MIT；这不替代下列依赖的许可证。本发布使用开源 LGPLv3 选项下的 Qt/PySide6/Shiboken6，不表示拥有 Qt 商业许可。原 wheel 所附的商业许可选项文件原样保留，仅作原始发行材料记录。

| Component | Version | License / details |
| --- | --- | --- |
| Python | 3.14.5 | PSF and bundled notices, third-party/Python |
| PySide6-Essentials, Shiboken6 | 6.11.2 | LGPL-3.0; full GPLv3/LGPLv3 texts and source notices in third-party/qt-source-notices |
| Qt Core, Gui, Widgets, Network, Svg and image format plugins | 6.11.2 | LGPL-3.0 and included third-party licenses; QtBase, QtSvg, QtImageFormats sources supplied |
| Pillow | 12.2.0 | MIT-CMU and bundled notices |
| NumPy | 2.4.6 | BSD-3-Clause and bundled third-party licenses |
| PyInstaller / hooks | 6.22.3 / 2026.7 | Build tooling; PyInstaller GPL with bootloader distribution exception; supplied notices retained |
| Mesa llvmpipe / LLVM (`opengl32sw.dll`) | Qt-distributed build | MIT / Boost and component notices; third-party/Mesa/Qt-llvmpipe-attribution.html |
| Microsoft VC runtime | Bundled upstream runtime | Microsoft distributable components; see upstream redistribution terms |

Exact installed-package metadata is in `DEPENDENCIES.json`. Copyright and license texts from packages and the corresponding Qt sources are in `third-party/`. Qt includes additional third-party code; retain its individual notices.

## LGPL source access and replacement

The v0.5.5 GitHub Release provides `SnipBoard-0.5.5-Qt-Corresponding-Sources.zip` alongside the executable ZIP, at no additional charge. It contains unmodified official QtBase, QtSvg, QtImageFormats and PySide/Shiboken source archives for 6.11.2, including their build scripts. Exact upstream URLs and SHA-256 hashes are in `docs/DEPENDENCY_SOURCES.json` and inside that source ZIP. Library sources were not modified for this build.

Release: https://github.com/cjr050127-bit/SnipBoard/releases/tag/v0.5.5

The portable application dynamically loads Qt/PySide/Shiboken libraries from `_internal`. Users may modify and replace the LGPL libraries, and reverse engineer the combined work as needed to debug those modifications. SnipBoard imposes no restriction on these LGPL rights and does not enforce a signature check against replacement libraries. Back up the application first; replacements must match Windows x64, CPython/ABI and the Qt interface used by the application. Replace relevant DLLs/PYD files and Qt plugins in `_internal/PySide6` and `_internal/shiboken6`. For incompatible ABI changes, rebuild SnipBoard with the replacement bindings and the included `SnipBoard-Qt.spec` / `build.ps1`. The full application source is also provided under MIT.

Build documentation: https://doc.qt.io/qtforpython-6/building_from_source/index.html
Qt build documentation: https://doc.qt.io/qt-6/build-sources.html
License obligations: https://www.qt.io/development/open-source-lgpl-obligations

The manifest checks integrity; it does not prevent replacement. This inventory and supplied materials are engineering compliance work, not a legal certification. No Snipaste software is included.
