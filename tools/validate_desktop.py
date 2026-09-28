"""Read-only real paste-group verification and offscreen visual review."""
import os
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
from pathlib import Path
import hashlib
import json
import sys
import tempfile
import time
import traceback
from PySide6.QtWidgets import QApplication
from PySide6.QtTest import QTest
from snipboard.source import discover, GROUP_ID
from snipboard.qt_app import Controller, configure_app
from snipboard.qt_support import STYLE


def fingerprint(source):
    paths = [source / 'history' / '.sp0']
    for folder in (source / 'history').iterdir():
        if GROUP_ID.fullmatch(folder.name) and folder.is_dir():
            paths.extend(p for p in folder.rglob('*') if p.is_file())
    return {str(p.relative_to(source)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths if p.exists()}


def main():
    sources = discover()
    if len(sources) != 1:
        raise RuntimeError('Expected one source for read-only verification')
    source = sources[0]
    before = fingerprint(source)
    app = QApplication.instance() or QApplication([])
    configure_app(app)
    errors = []
    def on_error(*args):
        errors.append(''.join(traceback.format_exception(*args)))
        sys.__excepthook__(*args)
    sys.excepthook = on_error
    output = Path(__file__).resolve().parents[1] / 'docs' / 'screenshots-0.5.0'
    output.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='snipboard-review-') as directory:
        controller = Controller(app, source, Path(directory), native=False)
        controller.timer.stop()
        started = time.perf_counter()
        report = controller.catalog.sync_source(source)
        report['initial_index_seconds'] = round(time.perf_counter() - started, 3)
        repeat = controller.catalog.sync_source(source)
        if report['errors'] or repeat['added']:
            raise AssertionError((report, repeat))
        def drain():
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                app.processEvents()
                QTest.qWait(20)
                if not controller.jobs.pending:
                    app.processEvents()
                    if not controller.jobs.pending:
                        return
            raise RuntimeError('UI workers did not finish')
        rows = controller.catalog.query()
        work_group = controller.catalog.create_work_group('作品参考 · 示例', [r['digest'] for r in rows[:8]])
        controller.catalog.set_current_work_group(work_group)
        for row in rows[:3]:
            controller.catalog.set_tags(row['digest'], ['构图参考', '待整理'])
        window = controller.new_library()
        window.resize(1500, 960)
        drain()
        window.grab().save(str(output / 'library-grid.png'))
        window.show_photo(window.rows[0])
        drain()
        window.grab().save(str(output / 'library-detail.png'))
        board = controller.show_board()
        assert board.board_id == f'work:{work_group}'
        board.resize(1100, 760)
        board.fit()
        drain()
        board.grab().save(str(output / 'group-viewer.png'))
        report.update(unique_images=len(controller.catalog.query()), repeat_added=repeat['added'],
                      screenshot_history_excluded=True, qt_callback_errors=errors,
                      source_group_files_unchanged=before == fingerprint(source),
                      screenshots=[str(p) for p in output.glob('*.png')])
        controller.quit()
        drain()
        controller._finish_shutdown()
        app.processEvents()
    if errors or not report['source_group_files_unchanged']:
        raise AssertionError(report)
    (output.parent / 'DESKTOP_VALIDATION_0_5_0.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=True))


if __name__ == '__main__':
    main()
