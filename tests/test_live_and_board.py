from datetime import datetime
from pathlib import Path
import hashlib
import sqlite3
import tempfile
import unittest
import zipfile

from snipboard.library import Library
from snipboard.live import parse_live


class LiveStateTests(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2026, 9, 22, 21, 0, 0)
        self.history = Path("D:/source/history")

    def test_only_completed_restore_establishes_experimental_state(self):
        log = (
            "[2026-09-22 21:24:55.900] [I] Initializing Snipaste...\n"
            "[2026-09-22 21:24:55.910] [I] History dir: D:\\source\\history\n"
            "[2026-09-22 21:24:55.946] [I] Request to switch to group 1\n"
            "[2026-09-22 21:24:55.950] [I] About to restore pasters...\n"
            "[2026-09-22 21:24:55.956] [I] Group 1 [Y5CXIX]: \"伸手\"\n"
            "[2026-09-22 21:24:57.311] [I] Pasters restored\n")
        result = parse_live(log, self.start, self.history)
        self.assertEqual("observed", result["status"])
        self.assertEqual("Y5CXIX", result["id"])
        self.assertFalse(result["release_qualified"])

    def test_partial_tail_and_incomplete_switch_clear_stale_state(self):
        complete = (
            "[2026-09-22 21:24:55.900] [I] Initializing Snipaste...\n"
            "[2026-09-22 21:24:55.910] [I] History dir: D:\\source\\history\n"
            "[2026-09-22 21:24:55.950] [I] About to restore pasters...\n"
            "[2026-09-22 21:24:55.956] [I] Group 1 [Y5CXIX]: \"伸手\"\n"
            "[2026-09-22 21:24:57.311] [I] Pasters restored\n")
        partial = "[2026-09-22 21:25:14.489] [I] Request to switch to group 3"
        self.assertEqual("observed", parse_live(complete + partial, self.start, self.history)["status"])
        self.assertEqual("unavailable", parse_live(complete + partial + "\n", self.start, self.history)["status"])

    def test_other_history_directory_never_establishes_state(self):
        log = (
            "[2026-09-22 21:24:55.900] [I] Initializing Snipaste...\n"
            "[2026-09-22 21:24:55.910] [I] History dir: D:\\other\\history\n"
            "[2026-09-22 21:24:55.950] [I] About to restore pasters...\n"
            "[2026-09-22 21:24:55.956] [I] Group 1 [Y5CXIX]: \"伸手\"\n"
            "[2026-09-22 21:24:57.311] [I] Pasters restored\n")
        self.assertEqual("unavailable", parse_live(log, self.start, self.history)["status"])


class BoardPersistenceTests(unittest.TestCase):
    def test_positions_viewport_subscription_and_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with Library(root / "library") as library:
                digest = hashlib.sha256(b"asset").hexdigest()
                with library.db:
                    board_id = library.db.execute(
                        "INSERT INTO boards(source,group_id,name) VALUES(?,?,?)",
                        ("D:/source", "ABC123", "Board")).lastrowid
                    item_id = library.db.execute(
                        "INSERT INTO items(board_id,digest,source_name) VALUES(?,?,?)",
                        (board_id, digest, "image.png")).lastrowid
                (library.root / "assets" / f"{digest}.png").write_bytes(b"asset")
                library.save_positions(board_id, {item_id: (12.5, 30.0, 280.0)})
                library.save_viewport(board_id, 44.0, 55.0, 1.25)
                library.subscribe(board_id, False)
                self.assertEqual((44.0, 55.0, 1.25), library.viewport(board_id))
                item = library.items(board_id)[0]
                self.assertEqual((12.5, 30.0, 280.0), (item["x"], item["y"], item["width"]))
                self.assertEqual(0, library.boards()[0]["subscribed"])
                target = root / "backup.zip"
                library.backup(target)
                with zipfile.ZipFile(target) as archive:
                    self.assertIn("library.sqlite3", archive.namelist())
                    self.assertIn(f"assets/{digest}.png", archive.namelist())
                    with tempfile.TemporaryDirectory() as extracted:
                        archive.extract("library.sqlite3", extracted)
                        db = sqlite3.connect(Path(extracted) / "library.sqlite3")
                        try:
                            self.assertEqual(1, db.execute("SELECT COUNT(*) FROM items").fetchone()[0])
                        finally:
                            db.close()


if __name__ == "__main__":
    unittest.main()
