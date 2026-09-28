from pathlib import Path
import tempfile
import unittest

from snipboard.launcher import main as launcher_main
from snipboard.library import Library
from snipboard.runtime import app_data_root, close_logging, configure_logging


class ReleaseTests(unittest.TestCase):
    def test_app_data_path_is_independent_of_working_directory(self):
        path = app_data_root(
            {"LOCALAPPDATA": "D:/Users/test/AppData/Local"}, Path("C:/fallback")
        )
        self.assertEqual(
            Path("D:/Users/test/AppData/Local/SnipBoard").resolve(), path
        )

    def test_logging_and_headless_packaged_self_test(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = configure_logging(root)
            try:
                self.assertEqual(root / "logs" / "snipboard.log", log)
                self.assertEqual(
                    0, launcher_main(["--library", str(root / "library"), "--self-test"])
                )
                self.assertTrue((root / "library" / "library.sqlite3").is_file())
            finally:
                close_logging()

    def test_library_uses_wal_for_ui_worker_concurrency(self):
        with tempfile.TemporaryDirectory() as directory:
            with Library(Path(directory)) as first, Library(Path(directory)) as second:
                self.assertEqual(
                    "wal", first.db.execute("PRAGMA journal_mode").fetchone()[0]
                )
                with first.db:
                    first.db.execute(
                        "INSERT INTO boards(source,group_id,name) VALUES(?,?,?)",
                        ("D:/source", "ABC123", "Board"),
                    )
                self.assertEqual(1, len(second.boards()))


if __name__ == "__main__":
    unittest.main()
