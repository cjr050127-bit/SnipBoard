"""Check automatic first-run connection using an owned process and temporary library."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time

from snipboard.discovery import discover_candidates
from snipboard.source import history_directory

executable = Path(sys.argv[1]).resolve()
candidates = discover_candidates()
if len(candidates) != 1:
    raise RuntimeError('This live integration check requires exactly one readable local Snipaste source')
with tempfile.TemporaryDirectory(prefix='snipboard-first-connect-') as temporary:
    root = Path(temporary)
    process = subprocess.Popen([str(executable), '--library', str(root)],
        env=dict(os.environ, QT_QPA_PLATFORM='offscreen'), creationflags=subprocess.CREATE_NO_WINDOW,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError('Application exited during first-run connection')
            database = root / 'library.sqlite3'
            if database.exists():
                connection = None
                try:
                    connection = sqlite3.connect(database, timeout=.2)
                    prefs = dict(connection.execute("SELECT key,value FROM preferences WHERE key IN ('source','connection.setup_done')"))
                    if prefs.get('connection.setup_done') == 'true' and prefs.get('source'):
                        path = Path(json.loads(prefs['source']))
                        assert history_directory(path) == Path(candidates[0]['history'])
                        report = dict(passed=True, automatic_first_connection=True,
                            isolated_library=True, candidate_count=1,
                            scope='packaged executable; real local discovery; source preference persisted without --source or manual input')
                        break
                except sqlite3.OperationalError:
                    pass
                finally:
                    if connection is not None:
                        connection.close()
            time.sleep(.1)
        else:
            raise RuntimeError('First-run automatic connection timed out')
    finally:
        # Stop only the exact child created by this check; never touch the user's application.
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=10)
destination = Path(__file__).resolve().parents[1] / 'docs/connection-0.5.2/packaged-first-run.json'
destination.parent.mkdir(exist_ok=True)
destination.write_text(json.dumps(report, indent=2), encoding='utf-8')
print(json.dumps(report))
