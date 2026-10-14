"""GUI-only entry point used by the packaged Windows executable."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

from snipboard.qt_app import run
from snipboard.catalog import Catalog
from snipboard.runtime import close_logging, configure_logging, default_library_dir


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="SnipBoard")
    parser.add_argument("--source", type=Path)
    parser.add_argument("--library", type=Path)
    parser.add_argument("--self-test", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    library_root = (args.library or default_library_dir()).expanduser().resolve()
    configure_logging(library_root if args.self_test else None)
    if args.self_test:
        import os
        import time
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PySide6.QtWidgets import QApplication
        from PIL import Image
        from snipboard.qt_app import Controller, configure_app
        app = QApplication.instance() or QApplication([])
        configure_app(app)
        source = library_root / '_selftest_source'
        group = source / 'history' / 'ABC123'
        group.mkdir(parents=True, exist_ok=True)
        for index, color in enumerate(('#e34242', '#ec8c32')):
            Image.new('RGB', (120 + index, 80), color).save(group / f'{index}.png')
        controller = Controller(app, source, library_root, native=False)
        controller.timer.stop()
        controller.catalog.sync_source(source)
        def drain():
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                app.processEvents()
                time.sleep(.01)
                if not controller.jobs.pending:
                    app.processEvents()
                    if not controller.jobs.pending:
                        return
            raise RuntimeError('GUI self-test worker timeout')
        window = controller.new_library()
        drain()
        if len(window.rows) != 2:
            raise RuntimeError('GUI self-test catalog failed')
        window.show_photo(window.rows[0])
        board = controller.show_board()
        drain()
        if window.grab().isNull() or board.grab().isNull() or len(board.pictures) != 2:
            raise RuntimeError('GUI self-test rendering failed')
        board.commands['紧凑排列'].trigger()
        drain()
        if board.compact_gap != 3 or not board.arrange_result or not board.arrange_result['positions']:
            raise RuntimeError('GUI self-test compact layout failed')
        for picture in board.pictures.values():
            bounds = board.mapFromScene(picture.sceneBoundingRect()).boundingRect()
            if not board.viewport().rect().contains(bounds):
                raise RuntimeError('GUI self-test compact viewport bounds failed')
        if len(window.rec_views) != 2 or not window.rec_models[0].rows:
            raise RuntimeError('GUI self-test recommendations failed')
        for grid in [window.grid, *window.rec_views]:
            if not grid.dragEnabled():
                raise RuntimeError('GUI self-test thumbnail drag disabled')
        if window.grid.drag_preview(window.rows[0], 1).isNull():
            raise RuntimeError('GUI self-test drag preview failed')
        digest = window.rows[0]['digest']
        window.tag_editor.setText('self-test tag')
        window.save_tags()
        if controller.catalog.tags_for(digest) != ['self-test tag']:
            raise RuntimeError('GUI self-test tags failed')
        group_id = controller.catalog.create_work_group('Self-test work group', [digest])
        controller.catalog.set_current_work_group(group_id)
        controller.show_board()
        if board.board_id != f'work:{group_id}' or len(board.pictures) != 1:
            raise RuntimeError('GUI self-test current work group failed')
        Image.new('RGB', (91, 173), '#338acf').save(group / 'auto-added.png')
        report = controller.catalog.sync_source(source)
        board.update_groups()
        if report['auto_added'] != 1 or len(board.pictures) != 2:
            raise RuntimeError('GUI self-test automatic collection failed')
        picture = next(iter(board.pictures.values()))
        picture.setSelected(True)
        digest = picture.row['digest']
        board.rotate(90)
        board.save()
        board.load_group(board.board_id)
        if board.pictures[digest].rotation() != 90:
            raise RuntimeError('GUI self-test rotation persistence failed')
        board.place_window('center')
        if board.grab().isNull():
            raise RuntimeError('GUI self-test rotated rendering failed')
        window.delete_work(group_id)
        drain()
        if (controller.catalog.work_groups() or controller.catalog.get_setting('work.current') is not None
                or controller.catalog.layout(f'work:{group_id}') is not None
                or board.board_id == f'work:{group_id}' or len(controller.catalog.query()) != 3):
            raise RuntimeError('GUI self-test reference group deletion failed')
        local_files = []
        for suffix, color in [('jpg', '#21b751'), ('webp', '#d329ac')]:
            path = library_root / f'_selftest_import.{suffix}'
            Image.new('RGB', (64, 93), color).save(path)
            local_files.append(path)
        imported = controller.catalog.import_images(local_files)
        if imported['added'] != 2 or imported['errors'] or len(controller.catalog.query()) != 5:
            raise RuntimeError('GUI self-test local image import failed')
        window.select_group(imported['board_id'])
        controller.show_board(imported['board_id'])
        drain()
        if len(window.rows) != 2 or len(board.pictures) != 2 or board.grab().isNull():
            raise RuntimeError('GUI self-test imported images rendering failed')
        deleted = window.rows[0]['digest']
        controller.delete_images([deleted])
        window.remove_label(imported['board_id'])
        source_label = next(g['id'] for g in controller.catalog.label_rows() if g['kind'] == 'source')
        window.remove_label(source_label)
        controller.catalog.sync_source(source)
        drain()
        if (controller.catalog.has_image(deleted) or len(controller.catalog.query()) != 4
                or source_label in [g['id'] for g in controller.catalog.label_rows()]):
            raise RuntimeError('GUI self-test image and automatic label removal failed')
        from snipboard.maintenance import inspect_library, restore_backup
        check = inspect_library(controller.catalog)
        if check['database'] or check['missing'] or check['corrupt']:
            raise RuntimeError('GUI self-test integrity check failed')
        backup_file = library_root / '_selftest_backup.zip'
        controller.catalog.backup(backup_file)
        recovered = library_root / '_selftest_restored'
        restore_backup(backup_file, recovered, Catalog.schema_version)
        with Catalog(recovered) as restored:
            if len(restored.query()) != 4 or inspect_library(restored)['thumbnails']:
                raise RuntimeError('GUI self-test backup recovery failed')
            restored.restore_images([deleted])
            if not restored.has_image(deleted):
                raise RuntimeError('GUI self-test trash recovery failed')
        from snipboard.source import normalize_source
        from snipboard.qt_connection import ConnectionDialog
        executable = source / 'Snipaste.exe'
        executable.touch()
        if normalize_source(executable) != source:
            raise RuntimeError('GUI self-test portable source resolution failed')
        connection = ConnectionDialog(controller)
        connection.found([dict(path=str(source), history=str(source / 'history'), reason='自检便携版', groups=1)])
        if connection.locations.count() != 1 or connection.grab().isNull():
            raise RuntimeError('GUI self-test connection guide rendering failed')
        connection.reject()
        drain()
        board.background_actions['transparent'].trigger()
        board.arm_window_tool('resize')
        if not board.glow_active() or board.viewport().grab().isNull() or board.windowOpacity() != 1:
            raise RuntimeError('GUI self-test transparent background and edge glow failed')
        board.window_tool = None
        board.background_actions['white'].trigger()
        if board.glow_active() or board.backgroundBrush().color().name() != '#ffffff':
            raise RuntimeError('GUI self-test background switching failed')
        board.set_background('black')
        window.clear_filters()
        drain()
        window.finder_panel.query.setPlainText('找横图')
        window.finder_panel.submit()
        drain()
        if not window.rows or any(row['width'] / row['height'] <= 1.05 for row in window.rows):
            raise RuntimeError('GUI self-test natural-language filtering failed')
        from snipboard.qt_model_settings import ModelSettingsDialog
        from snipboard.model_search import ModelConfig, ModelSearchResult
        import snipboard.qt_library as library_module
        settings = ModelSettingsDialog(window)
        if settings.consent.isChecked() or settings.apply_button.isEnabled() or settings.grab().isNull():
            raise RuntimeError('GUI self-test model settings failed')
        settings.reject()
        selected_row = dict(window.rows[0])
        model_calls = []
        def simulated_model(catalog, plan, query, config, cancel, **scope):
            model_calls.append(query)
            return ModelSearchResult([selected_row], 1, 4, 1,
                reasons={selected_row['digest']: '模型接口自检结果'})
        original_model = library_module.search_with_model
        try:
            library_module.search_with_model = simulated_model
            window.finder_model_config = ModelConfig('http://localhost:11434/v1', 'self-test-vision')
            window.finder_panel.set_model(window.finder_model_config)
            window.finder_panel.mode.setCurrentIndex(1)
            window.finder_panel.query.setPlainText('未标注的侧脸朝左')
            window.finder_panel.submit()
            drain()
            if (len(model_calls) != 1 or len(window.rows) != 1 or not window.finder_applied_model or
                    '已分析 1 / 4' not in window.summary.text()):
                raise RuntimeError('GUI self-test optional model flow failed')
        finally:
            library_module.search_with_model = original_model
        window.clear_finder()
        drain()
        import tempfile
        with tempfile.TemporaryDirectory(prefix='folder-selftest-', dir=library_root.parent) as temporary:
            reference = Path(temporary) / '文件夹参考'
            nested = reference / '子文件夹'
            nested.mkdir(parents=True)
            Image.new('RGB', (77, 95), '#449887').save(reference / 'first.jpg')
            Image.new('RGB', (95, 77), '#987744').save(nested / 'second.png')
            report = controller.catalog.import_folder(reference)
            if report['added'] != 2 or report['errors'] or not report['label_id']:
                raise RuntimeError('GUI self-test folder import failed')
            controller.changed.emit()
            window.select_group(report['label_id'])
            drain()
            if len(window.rows) != 2 or any('文件夹参考' not in row['manual_tags'] for row in window.rows):
                raise RuntimeError('GUI self-test folder label group failed')
        if os.name == 'nt':
            import errno
            from snipboard.atomic_io import replace_local_file
            with tempfile.TemporaryDirectory(prefix='atomic-selftest-', dir=library_root) as temporary:
                stage = Path(temporary) / 'stage.tmp'
                target = stage.with_name('published.bin')
                stage.write_bytes(b'atomic publication self-test')
                original_replace = os.replace
                attempts = []
                def cross_volume_once(source, destination):
                    attempts.append((source, destination))
                    if len(attempts) == 1:
                        raise OSError(errno.EXDEV, 'Simulated Windows path redirection')
                    return original_replace(source, destination)
                try:
                    os.replace = cross_volume_once
                    replace_local_file(stage, target)
                finally:
                    os.replace = original_replace
                if len(attempts) != 2 or target.read_bytes() != b'atomic publication self-test':
                    raise RuntimeError('GUI self-test redirected file publication failed')
        controller.quit()
        drain()
        controller._finish_shutdown()
        close_logging()
        return 0
    try:
        run(args.source, library_root)
        return 0
    except Exception as error:
        import logging
        from PySide6.QtWidgets import QApplication, QMessageBox
        app = QApplication.instance() or QApplication([])
        logging.getLogger(__name__).exception('Application startup failed')
        QMessageBox.critical(None, 'SnipBoard 无法启动',
            f'无法打开图库：{library_root}\n\n{error}\n\n请检查磁盘权限、数据库版本或从备份恢复到新图库。')
        return 1
    finally:
        close_logging()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
