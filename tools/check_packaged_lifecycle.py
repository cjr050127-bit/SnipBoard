"""Single-instance and abrupt-exit recovery check in an isolated, offscreen library."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
from PIL import Image

exe = Path(sys.argv[1]).resolve()
report = {}
with tempfile.TemporaryDirectory(prefix='snipboard-lifecycle-') as directory:
    root = Path(directory)
    source = root / 'source' / 'history' / 'ABC123'
    source.mkdir(parents=True)
    Image.new('RGB', (120, 80), 'blue').save(source / 'image.png')
    library = root / 'library'
    args = [str(exe), '--library', str(library), '--source', str(source.parent.parent)]
    env = dict(os.environ, QT_QPA_PLATFORM='offscreen')
    def start():
        process = subprocess.Popen(args, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            until = time.monotonic() + 20
            while time.monotonic() < until:
                if process.poll() is not None:
                    raise RuntimeError(f'Application exited: {process.returncode}')
                if (library / 'library.sqlite3').exists():
                    connection = None
                    try:
                        connection = sqlite3.connect(library / 'library.sqlite3', timeout=.1)
                        if connection.execute('SELECT count(*) FROM source_files').fetchone()[0] == 1:
                            time.sleep(.5)
                            return process
                    except sqlite3.Error:
                        pass
                    finally:
                        if connection:
                            connection.close()
                time.sleep(.1)
            raise RuntimeError('Application startup timed out')
        except BaseException:
            process.terminate()
            process.wait(timeout=5)
            raise
    first = start()
    try:
        second = subprocess.run(args, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                creationflags=subprocess.CREATE_NO_WINDOW, timeout=10)
        assert second.returncode == 0 and first.poll() is None
        report['same_library_second_launch_exits_cleanly'] = True
    finally:
        first.terminate()
        first.wait(timeout=5)
    restarted = start()
    try:
        with sqlite3.connect(library / 'library.sqlite3') as connection:
            assert connection.execute('PRAGMA quick_check').fetchall() == [('ok',)]
            assert connection.execute('SELECT count(*) FROM items').fetchone()[0] == 1
        connection.close()
        report['restart_after_forced_exit_database_ok'] = True
    finally:
        restarted.terminate()
        restarted.wait(timeout=5)
report['scope'] = 'owned EXE processes, temporary library, offscreen windows; abrupt exit intentionally injected'
output = Path(__file__).resolve().parents[1] / 'docs' / 'quality-0.5.0' / 'packaged-lifecycle.json'
output.write_text(json.dumps(report, indent=2), encoding='utf-8')
print(json.dumps(report))
