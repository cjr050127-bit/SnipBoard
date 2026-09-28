"""Independent append-only image collection with a transactional index."""
from __future__ import annotations

from pathlib import Path
import hashlib
import json
import math
import os
import sqlite3
import struct
import tempfile
import zlib

from .source import GROUP_ID, stable_read, history_directory


def validate_png(data: bytes) -> None:
    """Reject partial writes and malformed chunk boundaries before collecting."""
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("Not a PNG image")
    offset, first, image_data = 8, True, False
    while offset + 12 <= len(data):
        length = struct.unpack_from(">I", data, offset)[0]
        end = offset + 12 + length
        if end > len(data):
            raise ValueError("Incomplete PNG chunk")
        kind = data[offset + 4:offset + 8]
        payload = data[offset + 8:offset + 8 + length]
        crc = struct.unpack_from(">I", data, end - 4)[0]
        if zlib.crc32(kind + payload) != crc:
            raise ValueError("PNG checksum mismatch")
        if first:
            if kind != b"IHDR" or length != 13 or not all(struct.unpack_from(">II", payload)):
                raise ValueError("Invalid PNG header")
            first = False
        image_data |= kind == b"IDAT"
        if kind == b"IEND":
            if length or end != len(data) or not image_data:
                raise ValueError("Invalid PNG ending")
            return
        offset = end
    raise ValueError("Missing PNG ending")


class Library:
    schema_version = 2
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "assets").mkdir(exist_ok=True)
        self.db = sqlite3.connect(self.root / "library.sqlite3")
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        if self.db.execute('PRAGMA user_version').fetchone()[0] > self.schema_version:
            self.db.close()
            raise ValueError('This library requires a newer SnipBoard version')
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS boards (
                id INTEGER PRIMARY KEY, source TEXT NOT NULL, group_id TEXT NOT NULL,
                name TEXT NOT NULL, UNIQUE(source, group_id));
            CREATE TABLE IF NOT EXISTS items (
                id INTEGER PRIMARY KEY, board_id INTEGER NOT NULL REFERENCES boards(id),
                digest TEXT NOT NULL, source_name TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT '', tags TEXT NOT NULL DEFAULT '[]',
                collected_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(board_id, digest));
        """)
        with self.db:
            columns = {row[1] for row in self.db.execute('PRAGMA table_info(boards)')}
            if 'subscribed' not in columns:
                self.db.execute('ALTER TABLE boards ADD COLUMN subscribed INTEGER NOT NULL DEFAULT 1')
            self.db.execute('''CREATE TABLE IF NOT EXISTS positions (
                item_id INTEGER PRIMARY KEY REFERENCES items(id), x REAL NOT NULL,
                y REAL NOT NULL, width REAL NOT NULL)''')
            self.db.execute('''CREATE TABLE IF NOT EXISTS viewports (
                board_id INTEGER PRIMARY KEY REFERENCES boards(id),
                x REAL NOT NULL, y REAL NOT NULL, zoom REAL NOT NULL)''')
            if self.db.execute('PRAGMA user_version').fetchone()[0] < 1:
                self.db.execute('PRAGMA user_version=1')

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def collect(self, state: Path, group_id: str, name: str | None = None,
                unchanged: dict | None = None, cancelled=None) -> dict:
        if not GROUP_ID.fullmatch(group_id):
            raise ValueError("Invalid group id")
        source = state.resolve()
        if self.root == source or self.root.is_relative_to(source):
            raise ValueError("Library must be outside the Snipaste source directory")
        history = history_directory(source)
        if self.root.is_relative_to(history):
            raise ValueError("Library must be outside the Snipaste history directory")
        folder = history / group_id
        if not folder.is_dir() or folder.is_symlink() or not folder.resolve().is_relative_to(history):
            raise ValueError("Group directory does not exist or is external")
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO boards(source,group_id,name) VALUES(?,?,?)",
                            (str(source), group_id, name or group_id))
        board_id = self.db.execute("SELECT id FROM boards WHERE source=? AND group_id=?",
                                   (str(source), group_id)).fetchone()[0]
        added, errors = 0, []
        for image in sorted(folder.glob("*.png")):
            if cancelled and cancelled():
                break
            try:
                if image.is_symlink() or not image.resolve().is_relative_to(folder.resolve()):
                    raise ValueError("External image reference")
                before = image.stat()
                signature = (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                key = str(image)
                if unchanged is not None and unchanged.get(key) == signature:
                    continue
                data = stable_read(image, 128 * 1024 * 1024)
                validate_png(data)
                digest = hashlib.sha256(data).hexdigest()
                destination = self.root / "assets" / f"{digest}.png"
                if destination.exists():
                    if hashlib.sha256(destination.read_bytes()).hexdigest() != digest:
                        raise ValueError("Existing library asset is corrupt")
                else:
                    # Flush complete bytes before publishing; a crash leaves at worst an unindexed asset.
                    fd, temporary = tempfile.mkstemp(prefix=".import-", dir=destination.parent)
                    try:
                        with os.fdopen(fd, "wb") as stream:
                            stream.write(data)
                            stream.flush()
                            os.fsync(stream.fileno())
                        os.replace(temporary, destination)
                    finally:
                        Path(temporary).unlink(missing_ok=True)
                with self.db:
                    result = self.db.execute("INSERT OR IGNORE INTO items(board_id,digest,source_name) VALUES(?,?,?)",
                                             (board_id, digest, image.name))
                    added += result.rowcount
                after = image.stat()
                if unchanged is not None and signature == (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                    unchanged[key] = signature
            except (OSError, ValueError) as exc:
                errors.append({"file": image.name, "error": str(exc)})
        return {"board_id": board_id, "added": added, "errors": errors,
                "coverage": "persisted_png_files_only"}

    def annotate(self, item_id: int, note: str, tags: list[str]):
        normalized = sorted(set(t.strip() for t in tags if t.strip()))
        with self.db:
            result = self.db.execute("UPDATE items SET note=?,tags=? WHERE id=?",
                                     (note, json.dumps(normalized, ensure_ascii=False), item_id))
            if not result.rowcount:
                raise ValueError(f"Unknown item: {item_id}")

    def search(self, query: str = "") -> list[dict]:
        # Literal Unicode substring semantics; '%' and '_' are not SQL wildcard operators.
        needle = query.casefold()
        results = []
        for row in self.db.execute("SELECT items.*, boards.name AS board_name FROM items JOIN boards ON boards.id=items.board_id ORDER BY items.id"):
            item = dict(row)
            item["tags"] = json.loads(item["tags"])
            if not needle or any(needle in text.casefold() for text in [item["note"], *item["tags"]]):
                item["asset"] = str(self.root / "assets" / f"{item['digest']}.png")
                results.append(item)
        return results

    def boards(self) -> list[dict]:
        return [dict(row) for row in self.db.execute('''SELECT boards.*, COUNT(items.id) AS item_count
            FROM boards LEFT JOIN items ON items.board_id=boards.id
            GROUP BY boards.id ORDER BY boards.id''')]

    def subscribe(self, board_id: int, enabled: bool):
        with self.db:
            if not self.db.execute('UPDATE boards SET subscribed=? WHERE id=?', (int(enabled), board_id)).rowcount:
                raise ValueError('Unknown board')

    def items(self, board_id: int, query: str = '') -> list[dict]:
        positions = {row['item_id']: dict(row) for row in self.db.execute('SELECT * FROM positions')}
        items = [row for row in self.search() if row['board_id'] == board_id]
        for index, item in enumerate(items):
            item.update(positions.get(item['id'], {'x': (index % 4) * 300.0,
                        'y': (index // 4) * 280.0, 'width': 260.0}))
        needle = query.casefold()
        return [item for item in items if not needle or any(needle in text.casefold()
                for text in [item['note'], *item['tags']])]

    def save_positions(self, board_id: int, positions: dict):
        with self.db:
            for item_id, (x, y, width) in positions.items():
                if not all(math.isfinite(v) for v in (x, y, width)) or not 40 <= width <= 4000:
                    raise ValueError('Invalid image position or size')
                if not self.db.execute('SELECT 1 FROM items WHERE id=? AND board_id=?', (item_id, board_id)).fetchone():
                    raise ValueError('Item does not belong to this board')
                self.db.execute('INSERT OR REPLACE INTO positions VALUES(?,?,?,?)', (item_id, x, y, width))

    def viewport(self, board_id: int):
        row = self.db.execute('SELECT x,y,zoom FROM viewports WHERE board_id=?', (board_id,)).fetchone()
        return tuple(row) if row else (40.0, 40.0, 1.0)

    def save_viewport(self, board_id: int, x: float, y: float, zoom: float):
        if not all(math.isfinite(v) for v in (x, y, zoom)) or not 0.1 <= zoom <= 4:
            raise ValueError('Invalid viewport')
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO viewports VALUES(?,?,?,?)', (board_id, x, y, zoom))

    def backup(self, destination: Path, cancelled=lambda: False):
        '''Consistent SQLite snapshot; refuses to overwrite an existing destination.'''
        import zipfile
        import re
        destination = Path(destination).absolute()
        if destination.exists():
            raise FileExistsError('备份目标已存在，请选择新文件名')
        with tempfile.TemporaryDirectory(prefix='snipboard-backup-') as directory:
            snapshot = Path(directory) / 'library.sqlite3'
            target = sqlite3.connect(snapshot)
            try:
                self.db.backup(target)
                digests = [row[0] for row in target.execute('SELECT DISTINCT digest FROM items')]
            finally:
                # Connection.__exit__ commits but does not close. Windows
                # otherwise keeps the snapshot locked during temp cleanup.
                target.close()
            fd, name = tempfile.mkstemp(prefix='.snipboard-backup-', suffix='.partial', dir=destination.parent)
            os.close(fd)
            archive = Path(name)
            try:
                with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_STORED) as output:
                    output.write(snapshot, 'library.sqlite3')
                    for digest in digests:
                        if cancelled():
                            raise InterruptedError('备份已取消')
                        if not re.fullmatch(r'[0-9a-f]{64}', digest):
                            raise ValueError('Invalid asset identity')
                        asset = self.root / 'assets' / f'{digest}.png'
                        with asset.open('rb') as stream:
                            if hashlib.file_digest(stream, 'sha256').hexdigest() != digest:
                                raise ValueError(f'Corrupt asset: {digest}')
                        output.write(asset, f'assets/{digest}.png')
                with archive.open('r+b') as stream:
                    os.fsync(stream.fileno())
                if os.name == 'nt':
                    os.rename(archive, destination)
                else:
                    os.link(archive, destination)
            finally:
                archive.unlink(missing_ok=True)
