"""Versioned shared catalog, source inventory, and local feature index."""
from __future__ import annotations

import hashlib
import io
import math
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from datetime import datetime
import numpy as np
from PIL import Image

from .features import VERSION, extract, normalized_image, similarity, rgb_lab
from .library import Library, validate_png
from .source import inspect_source, stable_read, history_directory


class Catalog(Library):
    schema_version = 6
    local_source = 'snipboard:local-imports'
    def __init__(self, root):
        root = Path(root)
        database = root / 'library.sqlite3'
        if database.exists():
            with sqlite3.connect(database) as connection:
                version = connection.execute('PRAGMA user_version').fetchone()[0]
                if 0 < version < self.schema_version:
                    backup_dir = root / 'backups'
                    backup_dir.mkdir(exist_ok=True)
                    backup = backup_dir / f'before-v{self.schema_version}-{datetime.now():%Y%m%d-%H%M%S-%f}.sqlite3'
                    target = sqlite3.connect(backup)
                    try:
                        connection.backup(target)
                    finally:
                        target.close()
            connection.close()
        super().__init__(root)
        self.db.create_function('casefold', 1, lambda s: (s or '').casefold(), deterministic=True)
        previous_version = self.db.execute('PRAGMA user_version').fetchone()[0]
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS source_files (
                path TEXT PRIMARY KEY, board_id INTEGER NOT NULL REFERENCES boards(id),
                digest TEXT NOT NULL, signature TEXT NOT NULL, present INTEGER NOT NULL DEFAULT 1);
            CREATE INDEX IF NOT EXISTS source_members ON source_files(board_id,present,digest);
            CREATE INDEX IF NOT EXISTS item_digest ON items(digest);
            CREATE INDEX IF NOT EXISTS item_board ON items(board_id);
            CREATE TABLE IF NOT EXISTS features (
                digest TEXT PRIMARY KEY, version INTEGER NOT NULL, width INTEGER NOT NULL,
                height INTEGER NOT NULL, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS preferences (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS layouts (board_id INTEGER PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS manual_tags (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
            CREATE TABLE IF NOT EXISTS image_tags (
                digest TEXT NOT NULL, tag_id INTEGER NOT NULL REFERENCES manual_tags(id) ON DELETE CASCADE,
                PRIMARY KEY(digest,tag_id));
            CREATE INDEX IF NOT EXISTS tag_members ON image_tags(tag_id,digest);
            CREATE TABLE IF NOT EXISTS work_groups (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS work_members (
                group_id INTEGER NOT NULL REFERENCES work_groups(id) ON DELETE CASCADE, digest TEXT NOT NULL,
                added_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(group_id,digest));
            CREATE TABLE IF NOT EXISTS view_layouts (key TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS removed_labels (key TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS deleted_images (digest TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS label_members (
                board_id INTEGER NOT NULL REFERENCES boards(id) ON DELETE CASCADE,
                digest TEXT NOT NULL, PRIMARY KEY(board_id,digest));
            CREATE INDEX IF NOT EXISTS local_label_images ON label_members(digest);
            CREATE TABLE IF NOT EXISTS excluded_label_members (
                board_id INTEGER NOT NULL REFERENCES boards(id) ON DELETE CASCADE,
                digest TEXT NOT NULL, PRIMARY KEY(board_id,digest));
        ''')
        if previous_version < 3:
            with self.db:
                # Merge legacy per-source annotations onto the shared image identity.
                merged = {}
                for item in self.db.execute('SELECT digest,tags FROM items'):
                    try:
                        tags = json.loads(item['tags'])
                    except (TypeError, ValueError):
                        tags = []
                    if isinstance(tags, list):
                        merged.setdefault(item['digest'], set()).update(t.strip() for t in tags if isinstance(t, str) and t.strip())
                for digest, tags in merged.items():
                    self._write_tags(digest, sorted(tags))
                for source, in self.db.execute('SELECT DISTINCT source FROM boards'):
                    if self.db.execute('SELECT 1 FROM source_files LIMIT 1').fetchone():
                        self.db.execute('INSERT OR IGNORE INTO preferences VALUES(?,?)',
                                        ('monitor.ready:' + source, 'true'))
        if previous_version < self.schema_version:
            with self.db:
                self.db.execute(f'PRAGMA user_version={self.schema_version}')
        self.thumbnails = self.root / 'thumbnails'
        self.thumbnails.mkdir(exist_ok=True)

    def get_setting(self, key, default=None):
        row = self.db.execute('SELECT value FROM preferences WHERE key=?', (key,)).fetchone()
        try:
            value = json.loads(row[0]) if row else default
            if default is not None and not isinstance(value, type(default)):
                return default
            return value
        except (ValueError, TypeError):
            return default

    def set_setting(self, key, value):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO preferences VALUES(?,?)',
                            (key, json.dumps(value, ensure_ascii=False)))

    def layout(self, board_id):
        table, field = ('layouts', 'board_id') if isinstance(board_id, int) else ('view_layouts', 'key')
        row = self.db.execute(f'SELECT data FROM {table} WHERE {field}=?', (board_id,)).fetchone()
        try:
            value = json.loads(row[0]) if row else None
            if not isinstance(value, dict) or not isinstance(value.get('items', {}), dict):
                return None
            items = {}
            for key, item in value.get('items', {}).items():
                if not isinstance(item, dict):
                    continue
                numbers = [item.get(k) for k in ('x', 'y', 'w', 'h')]
                if not all(isinstance(n, (int, float)) and math.isfinite(n) and abs(n) <= 1e9 for n in numbers):
                    continue
                if not 0 < item['w'] <= 100000 or not 0 < item['h'] <= 100000:
                    continue
                item = dict(item)
                for name in ('angle', 'z', 'order'):
                    if name in item and (not isinstance(item[name], (int, float)) or not math.isfinite(item[name])):
                        item[name] = 0
                items[key] = item
            value['items'] = items
            viewport = value.get('viewport')
            if (not isinstance(viewport, list) or len(viewport) != 3 or
                    not all(isinstance(n, (int, float)) and math.isfinite(n) for n in viewport) or not .001 <= viewport[0] <= 100):
                value.pop('viewport', None)
            return value
        except (TypeError, ValueError):
            return None

    def save_layout(self, board_id, value):
        with self.db:
            if isinstance(board_id, str) and board_id.startswith('work:'):
                if not self.db.execute('SELECT 1 FROM work_groups WHERE id=?', (board_id.split(':')[1],)).fetchone():
                    return
            table = 'layouts' if isinstance(board_id, int) else 'view_layouts'
            self.db.execute(f'INSERT OR REPLACE INTO {table} VALUES(?,?)', (board_id, json.dumps(value)))

    def _asset(self, png):
        digest = hashlib.sha256(png).hexdigest()
        destination = self.root / 'assets' / f'{digest}.png'
        if destination.exists():
            if hashlib.sha256(destination.read_bytes()).hexdigest() != digest:
                raise ValueError('本地收藏副本损坏')
        else:
            fd, temporary = tempfile.mkstemp(dir=destination.parent, prefix='.import-')
            try:
                with os.fdopen(fd, 'wb') as output:
                    output.write(png)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, destination)
            finally:
                Path(temporary).unlink(missing_ok=True)
        return digest

    def ensure_feature(self, digest, force=False):
        row = self.db.execute('SELECT version FROM features WHERE digest=?', (digest,)).fetchone()
        thumb = self.thumbnails / f'{digest}-800.png'
        if not force and row and row[0] == VERSION and thumb.exists():
            return
        picture = normalized_image(self.root / 'assets' / f'{digest}.png')
        width, height = picture.size
        picture.thumbnail((1024, 1024))
        data = extract(picture)
        picture.thumbnail((800, 800))
        fd, temporary_name = tempfile.mkstemp(dir=self.thumbnails, prefix='.index-', suffix='.tmp')
        os.close(fd)
        temporary = Path(temporary_name)
        try:
            picture.save(temporary, format='PNG')
            os.replace(temporary, thumb)
        finally:
            temporary.unlink(missing_ok=True)
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO features VALUES(?,?,?,?,?)',
                            (digest, VERSION, width, height, json.dumps(data)))

    def sync_source(self, source, cancelled=lambda: False, progress=lambda *args: None):
        source = Path(source).resolve()
        history = history_directory(source)
        if self.root == source or self.root.is_relative_to(source) or self.root.is_relative_to(history):
            raise ValueError('图库必须位于 Snipaste 数据目录之外')
        snapshot = inspect_source(source)
        monitor_ready = self.get_setting('monitor.ready:' + str(source), False)
        groups = [(g['id'], g['name_candidate'] or g['id'], history / g['id']) for g in snapshot['groups']]
        report = {'groups': len(groups), 'files': 0, 'added': 0, 'auto_added': 0, 'errors': [], 'warnings': snapshot['warnings'],
                  'coverage': 'pasted-group PNG inventory only; screenshot history excluded'}
        for group_id, name, folder in groups:
            if cancelled():
                return {**report, 'cancelled': True}
            if folder.is_symlink() or not folder.resolve().is_relative_to(history.resolve()):
                report['errors'].append({'file': str(folder), 'error': '外部目录已跳过'})
                continue
            with self.db:
                self.db.execute('INSERT INTO boards(source,group_id,name) VALUES(?,?,?) '
                                'ON CONFLICT(source,group_id) DO UPDATE SET name=excluded.name',
                                (str(source), group_id, name))
            board = self.db.execute('SELECT id FROM boards WHERE source=? AND group_id=?',
                                    (str(source), group_id)).fetchone()[0]
            seen = set()
            try:
                files = sorted(folder.iterdir())
            except OSError as error:
                report['errors'].append({'file': str(folder), 'error': str(error)})
                continue
            for path in files:
                if cancelled():
                    return {**report, 'cancelled': True}
                if path.name.startswith('.') or not path.is_file():
                    continue
                if path.suffix.lower() != '.png':
                    report['errors'].append({'file': str(path), 'error': '暂不支持的源文件类型'})
                    continue
                key = str(path)
                seen.add(key)
                try:
                    if path.is_symlink() or not path.resolve().is_relative_to(history.resolve()):
                        raise ValueError('外部图片引用已跳过')
                    stat = path.stat()
                    signature = json.dumps([stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns])
                    old = self.db.execute('SELECT digest,signature,present FROM source_files WHERE path=?', (key,)).fetchone()
                    if old and old['signature'] == signature and (self.root / 'assets' / f"{old['digest']}.png").is_file():
                        digest = old['digest']
                    else:
                        content = stable_read(path, 128 * 1024 * 1024)
                        png = content
                        validate_png(png)
                        digest = self._asset(png)
                    self.ensure_feature(digest)
                    with self.db:
                        # Serialize the membership decision with changes to the active work group.
                        self.db.execute('BEGIN IMMEDIATE')
                        result = self.db.execute('INSERT OR IGNORE INTO items(board_id,digest,source_name) VALUES(?,?,?)',
                                                 (board, digest, path.name))
                        report['added'] += result.rowcount
                        self.db.execute('INSERT OR REPLACE INTO source_files VALUES(?,?,?,?,1)',
                                        (key, board, digest, signature))
                        is_new = old is None or old['digest'] != digest or not old['present']
                        capture_group = self.get_setting('work.current')
                        if (is_new and monitor_ready and capture_group and
                            not self.db.execute('SELECT 1 FROM deleted_images WHERE digest=?', (digest,)).fetchone() and
                            self.db.execute('SELECT 1 FROM work_groups WHERE id=?', (capture_group,)).fetchone()):
                            report['auto_added'] += self.db.execute(
                                'INSERT OR IGNORE INTO work_members(group_id,digest) VALUES(?,?)',
                                (capture_group, digest)).rowcount
                    report['files'] += 1
                    progress(report['files'], path.name)
                except (OSError, ValueError, SyntaxError) as error:
                    report['errors'].append({'file': key, 'error': str(error)})
            # Mark absent paths only after a successful complete enumeration. Copies remain.
            with self.db:
                for row in self.db.execute('SELECT path FROM source_files WHERE board_id=?', (board,)).fetchall():
                    if row['path'] not in seen:
                        self.db.execute('UPDATE source_files SET present=0 WHERE path=?', (row['path'],))
        known_ids = {group_id for group_id, _, _ in groups}
        with self.db:
            for row in self.db.execute('SELECT id,group_id FROM boards WHERE source=?', (str(source),)).fetchall():
                if row['group_id'] not in known_ids:
                    self.db.execute('UPDATE source_files SET present=0 WHERE board_id=?', (row['id'],))
        # Include older independent collections in the feature index without deleting metadata.
        for row in self.db.execute('SELECT DISTINCT digest FROM items').fetchall():
            if cancelled():
                return {**report, 'cancelled': True}
            try:
                self.ensure_feature(row['digest'])
            except (OSError, ValueError) as error:
                report['errors'].append({'file': row['digest'], 'error': str(error)})
        self.set_setting('last_sync', report)
        if not report['errors']:
            self.set_setting('monitor.ready:' + str(source), True)
        return report

    def import_images(self, paths, cancelled=lambda: False, progress=lambda *args: None):
        """Copy explicitly selected images into the library; never watch their folders."""
        report = dict(added=0, duplicates=0, errors=[], board_id=None, cancelled=False)
        for index, filename in enumerate(paths, 1):
            if cancelled():
                report['cancelled'] = True
                break
            path = Path(filename)
            try:
                content = stable_read(path, 128 * 1024 * 1024)
                with Image.open(io.BytesIO(content)) as source:
                    if source.format not in ('PNG', 'JPEG', 'WEBP', 'BMP', 'TIFF', 'GIF'):
                        raise ValueError('不支持此图片格式')
                    source.load()
                    png = source.format == 'PNG' and not getattr(source, 'is_animated', False)
                if png:
                    validate_png(content)
                else:
                    picture = normalized_image(io.BytesIO(content))
                    picture.info.clear()
                    output = io.BytesIO()
                    picture.save(output, format='PNG')
                    content = output.getvalue()
                digest = self._asset(content)
                self.ensure_feature(digest)
                with self.db:
                    self.db.execute('INSERT OR IGNORE INTO boards(source,group_id,name) VALUES(?,?,?)',
                                    (self.local_source, 'imports', '本地导入'))
                    board = self.db.execute('SELECT id FROM boards WHERE source=? AND group_id=?',
                                            (self.local_source, 'imports')).fetchone()[0]
                    inserted = self.db.execute('INSERT OR IGNORE INTO items(board_id,digest,source_name) VALUES(?,?,?)',
                                               (board, digest, path.name)).rowcount
                    restored = self.db.execute('DELETE FROM deleted_images WHERE digest=?', (digest,)).rowcount
                    self.db.execute('DELETE FROM removed_labels WHERE key=?', (str(board),))
                report['board_id'] = board
                report['added' if inserted or restored else 'duplicates'] += 1
            except (OSError, ValueError, SyntaxError, Image.DecompressionBombError) as error:
                report['errors'].append(dict(file=str(path), error=str(error)))
            progress(index, path.name)
        return report

    def query(self, boards=None, text='', archived=False, digests=None):
        removed = {r[0] for r in self.db.execute('SELECT key FROM removed_labels')}
        excluded = {(r[0], r[1]) for r in self.db.execute('SELECT board_id,digest FROM excluded_label_members')}
        where, params = ['NOT EXISTS(SELECT 1 FROM deleted_images d WHERE d.digest=i.digest)'], []
        if digests is not None:
            if not digests:
                return []
            where.append('i.digest IN (SELECT value FROM json_each(?))')
            params.append(json.dumps(list(digests)))
        if boards is not None:
            boards = [b for b in boards if str(b) not in removed]
            if not boards:
                return []
            where.append('i.board_id IN (' + ','.join('?' * len(boards)) + ')')
            params.extend(boards)
            where.append('NOT EXISTS(SELECT 1 FROM excluded_label_members e WHERE e.board_id=i.board_id AND e.digest=i.digest)')
        if archived:
            where.append('b.source<>?')
            params.append(self.local_source)
            where.append('NOT EXISTS(SELECT 1 FROM source_files s WHERE s.board_id=i.board_id AND s.digest=i.digest AND s.present=1)')
        elif archived is False:
            where.append('(b.source=? OR EXISTS(SELECT 1 FROM source_files s WHERE s.board_id=i.board_id AND s.digest=i.digest AND s.present=1))')
            params.append(self.local_source)
        if text:
            where.append("(instr(casefold(i.source_name||' '||i.note||' '||CASE WHEN EXISTS(SELECT 1 FROM removed_labels r "
                         "WHERE r.key=CAST(b.id AS TEXT)) OR EXISTS(SELECT 1 FROM excluded_label_members e WHERE e.board_id=b.id AND e.digest=i.digest) THEN '' ELSE b.name END),?)>0 OR EXISTS("
                         "SELECT 1 FROM image_tags mt JOIN manual_tags t ON t.id=mt.tag_id "
                         "WHERE mt.digest=i.digest AND instr(casefold(t.name),?)>0) OR EXISTS("
                         "SELECT 1 FROM label_members lm JOIN boards lb ON lb.id=lm.board_id "
                         "WHERE lm.digest=i.digest AND instr(casefold(lb.name),?)>0 AND NOT EXISTS("
                         "SELECT 1 FROM removed_labels r WHERE r.key=CAST(lb.id AS TEXT)) AND NOT EXISTS(SELECT 1 FROM excluded_label_members e WHERE e.board_id=lb.id AND e.digest=i.digest)))")
            params.extend([text.casefold()] * 3)
        sql = '''SELECT i.*, b.name AS board_name, b.group_id, b.source,
                 f.width,f.height,f.data AS feature FROM items i JOIN boards b ON b.id=i.board_id
                 LEFT JOIN features f ON f.digest=i.digest WHERE ''' + (' AND '.join(where) or '1') + ' ORDER BY i.id DESC'
        result = {}
        tag_map = {}
        for tag in self.db.execute('SELECT m.digest,t.name FROM image_tags m JOIN manual_tags t ON t.id=m.tag_id ORDER BY t.name'):
            tag_map.setdefault(tag['digest'], []).append(tag['name'])
        for row in self.db.execute(sql, params):
            item = dict(row)
            membership = [] if str(item['board_id']) in removed or (item['board_id'], item['digest']) in excluded else [[item['board_id'], item['board_name']]]
            if item['digest'] in result:
                result[item['digest']]['memberships'].extend(membership)
                continue
            item['feature'] = json.loads(item['feature']) if item['feature'] else None
            item['manual_tags'] = tag_map.get(item['digest'], [])
            item['tags'] = json.dumps(item['manual_tags'], ensure_ascii=False)
            item['memberships'] = membership
            item['asset'] = str(self.root / 'assets' / f"{item['digest']}.png")
            item['thumbnail'] = str(self.thumbnails / f"{item['digest']}-800.png")
            item['width'], item['height'] = item['width'] or 1, item['height'] or 1
            result[item['digest']] = item
        for member in self.db.execute('SELECT m.digest,b.id,b.name FROM label_members m JOIN boards b ON b.id=m.board_id'):
            if member['digest'] in result and str(member['id']) not in removed and (member['id'], member['digest']) not in excluded:
                memberships = result[member['digest']]['memberships']
                if not any(ident == member['id'] for ident, _ in memberships):
                    memberships.append([member['id'], member['name']])
        for item in result.values():
            item['board_name'] = item['memberships'][0][1] if item['memberships'] else '未分类'
        return list(result.values())

    def group_rows(self):
        return [dict(row) for row in self.db.execute('''SELECT b.*,
            CASE WHEN b.source=? THEN (SELECT count(*) FROM items i WHERE i.board_id=b.id
                AND NOT EXISTS(SELECT 1 FROM deleted_images d WHERE d.digest=i.digest))
            ELSE (SELECT count(DISTINCT s.digest) FROM source_files s WHERE s.board_id=b.id AND s.present=1
                AND NOT EXISTS(SELECT 1 FROM deleted_images d WHERE d.digest=s.digest)) END AS current_count,
            (SELECT count(*) FROM items i WHERE i.board_id=b.id
                AND NOT EXISTS(SELECT 1 FROM deleted_images d WHERE d.digest=i.digest)) AS item_count
            FROM boards b ORDER BY b.id''', (self.local_source,))]

    def tags_for(self, digest):
        return [row[0] for row in self.db.execute('''SELECT t.name FROM manual_tags t
            JOIN image_tags m ON m.tag_id=t.id WHERE m.digest=? ORDER BY t.name''', (digest,))]

    def _write_tags(self, digest, names):
        self.db.execute('DELETE FROM image_tags WHERE digest=?', (digest,))
        for name in names:
            self.db.execute('INSERT OR IGNORE INTO manual_tags(name) VALUES(?)', (name,))
            ident = self.db.execute('SELECT id FROM manual_tags WHERE name=?', (name,)).fetchone()[0]
            self.db.execute('DELETE FROM removed_labels WHERE key=?', (f'tag:{ident}',))
            self.db.execute('INSERT OR IGNORE INTO image_tags VALUES(?,?)', (digest, ident))
        self.db.execute('UPDATE items SET tags=? WHERE digest=?', (json.dumps(names, ensure_ascii=False), digest))

    def set_tags(self, digest, names):
        if not self.has_image(digest):
            raise ValueError('图片不在当前图库中')
        names = sorted(set(str(name).strip()[:80] for name in names if str(name).strip()))
        with self.db:
            self._write_tags(digest, names)

    def annotate(self, item_id, note, tags):
        row = self.db.execute('SELECT digest FROM items WHERE id=?', (item_id,)).fetchone()
        if row:
            self.set_tags(row['digest'], tags)
            with self.db:
                self.db.execute('UPDATE items SET note=? WHERE digest=?', (note, row['digest']))

    def work_groups(self):
        current = self.get_setting('work.current')
        return [dict(row, current=row['id'] == current) for row in self.db.execute('''
            SELECT w.*, (SELECT count(*) FROM work_members m WHERE m.group_id=w.id
                AND NOT EXISTS(SELECT 1 FROM deleted_images d WHERE d.digest=m.digest)) AS count
            FROM work_groups w ORDER BY w.id''')]

    def create_work_group(self, name, digests=()):
        name = name.strip()[:80]
        if not name:
            raise ValueError('请输入参考组名称')
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            maximum = self.db.execute('SELECT coalesce(max(id),0) FROM work_groups').fetchone()[0]
            ident = max(maximum, self.get_setting('work.last_id', 0)) + 1
            self.db.execute('INSERT INTO work_groups(id,name) VALUES(?,?)', (ident, name))
            self.db.execute('INSERT OR REPLACE INTO preferences VALUES(?,?)', ('work.last_id', json.dumps(ident)))
        self.add_to_work_group(ident, digests)
        return ident

    def rename_work_group(self, ident, name):
        if not name.strip():
            raise ValueError('名称不能为空')
        with self.db:
            self.db.execute('UPDATE work_groups SET name=? WHERE id=?', (name.strip()[:80], ident))

    def delete_work_group(self, ident):
        """Remove only group metadata and membership, never image assets or tags."""
        reference = f'work:{ident}'
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            if not self.db.execute('SELECT 1 FROM work_groups WHERE id=?', (ident,)).fetchone():
                return False
            snapshot = dict(name=self.db.execute('SELECT name FROM work_groups WHERE id=?', (ident,)).fetchone()[0],
                            members=[r[0] for r in self.db.execute('SELECT digest FROM work_members WHERE group_id=?', (ident,))],
                            layout=self.layout(reference))
            self.db.execute('INSERT OR REPLACE INTO preferences VALUES(?,?)', (f'deleted-work:{ident}', json.dumps(snapshot)))
            # Do not reuse deleted IDs referenced by old window sessions.
            last_id = max(ident, self.get_setting('work.last_id', 0))
            self.db.execute('INSERT OR REPLACE INTO preferences VALUES(?,?)', ('work.last_id', json.dumps(last_id)))
            self.db.execute('DELETE FROM work_groups WHERE id=?', (ident,))
            self.db.execute('DELETE FROM view_layouts WHERE key=?', (reference,))
            if self.get_setting('work.current') == ident:
                self.db.execute('INSERT OR REPLACE INTO preferences VALUES(?,?)', ('work.current', 'null'))
                epoch = self.get_setting('work.epoch', 0) + 1
                self.db.execute('INSERT OR REPLACE INTO preferences VALUES(?,?)', ('work.epoch', json.dumps(epoch)))
            if self.get_setting('board.last_group') == reference:
                self.db.execute('DELETE FROM preferences WHERE key=?', ('board.last_group',))
            states = self.get_setting('windows', [])
            for state in states:
                if state.get('work_group') == ident:
                    state['work_group'] = None
            self.db.execute('INSERT OR REPLACE INTO preferences VALUES(?,?)', ('windows', json.dumps(states)))
        return True

    def set_current_work_group(self, ident):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            if ident is not None and not self.db.execute('SELECT 1 FROM work_groups WHERE id=?', (ident,)).fetchone():
                raise ValueError('参考组不存在')
            epoch = self.get_setting('work.epoch', 0) + 1
            self.db.execute('INSERT OR REPLACE INTO preferences VALUES(?,?)', ('work.current', json.dumps(ident)))
            self.db.execute('INSERT OR REPLACE INTO preferences VALUES(?,?)', ('work.epoch', json.dumps(epoch)))

    def add_to_work_group(self, ident, digests):
        if not self.db.execute('SELECT 1 FROM work_groups WHERE id=?', (ident,)).fetchone():
            raise ValueError('参考组不存在')
        added = 0
        with self.db:
            for digest in set(digests):
                if self.has_image(digest):
                    added += self.db.execute('INSERT OR IGNORE INTO work_members(group_id,digest) VALUES(?,?)',
                                             (ident, digest)).rowcount
        return added

    def remove_from_work_group(self, ident, digests):
        with self.db:
            self.db.executemany('DELETE FROM work_members WHERE group_id=? AND digest=?', [(ident, d) for d in digests])

    def label_rows(self):
        result = [dict(id=g['id'], name=g['name'], kind='local' if g['source'] == self.local_source else 'source', count=g['current_count']) for g in self.group_rows()]
        # Union source membership with local additions, counting each image once.
        counts = dict(self.db.execute('''SELECT m.board_id,count(DISTINCT m.digest) FROM (
            SELECT board_id,digest FROM source_files WHERE present=1
            UNION SELECT i.board_id,i.digest FROM items i JOIN boards b ON b.id=i.board_id WHERE b.source=?
            UNION SELECT board_id,digest FROM label_members) m
            WHERE EXISTS(SELECT 1 FROM items i WHERE i.digest=m.digest)
            AND NOT EXISTS(SELECT 1 FROM deleted_images d WHERE d.digest=m.digest)
            AND NOT EXISTS(SELECT 1 FROM excluded_label_members e WHERE e.board_id=m.board_id AND e.digest=m.digest)
            GROUP BY m.board_id''', (self.local_source,)))
        for group in result:
            group['count'] = counts.get(group['id'], 0)
        result += [dict(id=f"tag:{row['id']}", name=row['name'], kind='manual', count=row['count'])
                   for row in self.db.execute('''SELECT t.*, count(m.digest) AS count FROM manual_tags t
                       LEFT JOIN image_tags m ON m.tag_id=t.id
                       AND NOT EXISTS(SELECT 1 FROM deleted_images d WHERE d.digest=m.digest)
                       GROUP BY t.id ORDER BY t.name''')]
        removed = {r[0] for r in self.db.execute('SELECT key FROM removed_labels')}
        return [g for g in result if str(g['id']) not in removed]

    def add_to_label(self, reference, digests):
        """Add memberships locally without changing Snipaste's source inventory."""
        if not any(g['id'] == reference for g in self.label_rows()):
            raise ValueError('目标标签不存在或已移除')
        added = 0
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            existing = self._members(reference)
            for digest in set(digests):
                if not self.has_image(digest):
                    continue
                if isinstance(reference, int):
                    # An explicit re-add overrides a previous local exclusion.
                    self.db.execute('DELETE FROM excluded_label_members WHERE board_id=? AND digest=?', (reference, digest))
                    self.db.execute('INSERT OR IGNORE INTO label_members VALUES(?,?)', (reference, digest))
                else:
                    self.db.execute('INSERT OR IGNORE INTO image_tags VALUES(?,?)', (digest, int(reference[4:])))
                    self.db.execute('UPDATE items SET tags=? WHERE digest=?',
                                    (json.dumps(self.tags_for(digest), ensure_ascii=False), digest))
                added += digest not in existing
        return added

    def image_labels(self, digest):
        """Visible source/local and manual labels attached to one image."""
        rows = self.query(archived=None, digests=[digest])
        if not rows:
            return []
        row = rows[0]
        references = {ident for ident, _ in row['memberships']}
        names = set(row['manual_tags'])
        return [g for g in self.label_rows() if g['id'] in references
                or (g['kind'] == 'manual' and g['name'] in names)]

    def remove_from_label(self, reference, digest):
        """Detach only this image. Source memberships stay excluded across sync."""
        if not self.has_image(digest):
            return False
        if not any(g['id'] == reference for g in self.image_labels(digest)):
            return False
        with self.db:
            if isinstance(reference, int):
                self.db.execute('DELETE FROM label_members WHERE board_id=? AND digest=?', (reference, digest))
                self.db.execute('INSERT OR IGNORE INTO excluded_label_members VALUES(?,?)', (reference, digest))
            else:
                self.db.execute('DELETE FROM image_tags WHERE tag_id=? AND digest=?', (int(reference[4:]), digest))
                self.db.execute('UPDATE items SET tags=? WHERE digest=?',
                                (json.dumps(self.tags_for(digest), ensure_ascii=False), digest))
        return True

    def has_image(self, digest):
        return bool(self.db.execute('''SELECT 1 FROM items WHERE digest=?
            AND NOT EXISTS(SELECT 1 FROM deleted_images WHERE digest=?)''', (digest, digest)).fetchone())

    def delete_images(self, digests):
        """Library deletion is persistent across sync; source files and copies stay intact."""
        count = 0
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            for digest in set(digests):
                if self.has_image(digest):
                    count += self.db.execute('INSERT OR IGNORE INTO deleted_images VALUES(?)', (digest,)).rowcount
        return count

    def deleted_rows(self):
        return [dict(r) for r in self.db.execute('''SELECT d.digest,min(i.source_name) AS name FROM deleted_images d
            JOIN items i ON i.digest=d.digest GROUP BY d.digest ORDER BY name''')]

    def restore_images(self, digests):
        with self.db:
            return sum(self.db.execute('DELETE FROM deleted_images WHERE digest=?', (d,)).rowcount for d in set(digests))

    def removed_label_rows(self):
        result = []
        for key, in self.db.execute('SELECT key FROM removed_labels ORDER BY key'):
            if key.isdigit():
                reference = int(key)
                row = self.db.execute('SELECT name FROM boards WHERE id=?', (reference,)).fetchone()
            elif key.startswith('tag:') and key[4:].isdigit():
                reference = key
                row = self.db.execute('SELECT name FROM manual_tags WHERE id=?', (int(key[4:]),)).fetchone()
            else:
                continue
            if row:
                result.append(dict(id=reference, name=row[0], kind='manual' if isinstance(reference, str) else 'source'))
        return result

    def restore_labels(self, references):
        count = 0
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            for reference in references:
                restored = self.db.execute('DELETE FROM removed_labels WHERE key=?', (str(reference),)).rowcount
                count += restored
                if restored and isinstance(reference, str) and reference.startswith('tag:'):
                    for digest in self.get_setting('removed-members:' + reference, []):
                        if self.db.execute('SELECT 1 FROM items WHERE digest=?', (digest,)).fetchone():
                            self.db.execute('INSERT OR IGNORE INTO image_tags VALUES(?,?)', (digest, int(reference[4:])))
                            self.db.execute('UPDATE items SET tags=? WHERE digest=?', (json.dumps(self.tags_for(digest)), digest))
                    self.db.execute('DELETE FROM preferences WHERE key=?', ('removed-members:' + reference,))
        return count

    def deleted_work_rows(self):
        return [dict(id=int(r['key'].split(':')[1]), **json.loads(r['value']))
                for r in self.db.execute("SELECT key,value FROM preferences WHERE key LIKE 'deleted-work:%'")]

    def restore_work_groups(self, identities):
        count = 0
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            for ident in identities:
                saved = self.get_setting(f'deleted-work:{ident}')
                if not saved or self.db.execute('SELECT 1 FROM work_groups WHERE id=?', (ident,)).fetchone():
                    continue
                self.db.execute('INSERT INTO work_groups(id,name) VALUES(?,?)', (ident, saved['name']))
                for digest in saved['members']:
                    if self.db.execute('SELECT 1 FROM items WHERE digest=?', (digest,)).fetchone():
                        self.db.execute('INSERT OR IGNORE INTO work_members(group_id,digest) VALUES(?,?)', (ident, digest))
                if saved['layout']:
                    self.db.execute('INSERT OR REPLACE INTO view_layouts VALUES(?,?)', (f'work:{ident}', json.dumps(saved['layout'])))
                self.db.execute('DELETE FROM preferences WHERE key=?', (f'deleted-work:{ident}',))
                count += 1
        return count

    def remove_label(self, reference):
        if not any(g['id'] == reference for g in self.label_rows()):
            return False
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            self.db.execute('INSERT OR IGNORE INTO removed_labels VALUES(?)', (str(reference),))
            if isinstance(reference, str) and reference.startswith('tag:'):
                ident = int(reference.split(':')[1])
                affected = [r[0] for r in self.db.execute('SELECT digest FROM image_tags WHERE tag_id=?', (ident,))]
                self.db.execute('INSERT OR REPLACE INTO preferences VALUES(?,?)',
                                ('removed-members:' + reference, json.dumps(affected)))
                self.db.execute('DELETE FROM image_tags WHERE tag_id=?', (ident,))
                for digest in affected:
                    self.db.execute('UPDATE items SET tags=? WHERE digest=?', (json.dumps(self.tags_for(digest)), digest))
            if self.get_setting('board.last_group') == reference:
                self.db.execute('DELETE FROM preferences WHERE key=?', ('board.last_group',))
        return True

    def collection_rows(self):
        result = [dict(id=f"work:{g['id']}", name=g['name'], kind='work',
                       current_count=g['count'], item_count=g['count']) for g in self.work_groups()]
        result += [dict(id=g['id'], name=g['name'], kind=g['kind'], current_count=g['count'], item_count=g['count'])
                   for g in self.label_rows()]
        return result

    def _members(self, reference):
        if self.db.execute('SELECT 1 FROM removed_labels WHERE key=?', (str(reference),)).fetchone():
            return set()
        if isinstance(reference, int):
            return {r[0] for r in self.db.execute('''SELECT digest FROM source_files WHERE board_id=? AND present=1
                UNION SELECT i.digest FROM items i JOIN boards b ON b.id=i.board_id WHERE b.id=? AND b.source=?
                UNION SELECT digest FROM label_members WHERE board_id=?''',
                (reference, reference, self.local_source, reference))} - {r[0] for r in self.db.execute(
                    'SELECT digest FROM excluded_label_members WHERE board_id=?', (reference,))}
        kind, _, value = str(reference).partition(':')
        if not value.isdigit() or kind not in ('tag', 'work'):
            return set()
        table, key = ('image_tags', 'tag_id') if kind == 'tag' else ('work_members', 'group_id')
        return {r[0] for r in self.db.execute(f'SELECT digest FROM {table} WHERE {key}=?', (int(value),))}

    def query_filtered(self, labels=(), work_group=None, text='', archived=False, filters=None, match_all=True):
        labels = list(labels or [])
        # Explicit collection membership retains local copies after source removal.
        retained = work_group is not None or bool(labels)
        allowed = None
        if labels:
            members = [self._members(key) for key in labels]
            allowed = set.intersection(*members) if match_all else set.union(*members)
        if work_group is not None:
            members = self._members(f'work:{work_group}')
            allowed = members if allowed is None else allowed & members
        rows = self.query(text=text, archived=True if archived else None if retained else False, digests=allowed)
        return self.filter_properties(rows, filters or {})

    def collection_query(self, reference):
        if isinstance(reference, str) and reference.startswith('work:'):
            return self.query_filtered(work_group=int(reference.split(':')[1]))
        return self.query_filtered(labels=[reference])

    @staticmethod
    def filter_properties(rows, filters):
        def keep(row):
            w, h = row['width'], row['height']
            ratio = w / max(h, 1)
            orientation = filters.get('orientation', 'any')
            if orientation == 'landscape' and ratio <= 1.05: return False
            if orientation == 'portrait' and ratio >= .95: return False
            if orientation == 'square' and not .95 <= ratio <= 1.05: return False
            target = filters.get('ratio', 0)
            if target and abs(ratio / target - 1) > filters.get('ratio_tolerance', .05): return False
            for name, value in [('width', w), ('height', h)]:
                if value < filters.get('min_' + name, 0): return False
                maximum = filters.get('max_' + name, 0)
                if maximum and value > maximum: return False
            minimum, maximum = filters.get('brightness_min', 0), filters.get('brightness_max', 100)
            if minimum > 0 or maximum < 100:
                feature = row.get('feature') or {}
                brightness = feature.get('brightness')
                if brightness is None:
                    palette = feature.get('palette', [])
                    if not palette: return False
                    brightness = float(np.average(rgb_lab(np.array(palette)[:, :3])[:, 0], weights=np.array(palette)[:, 3]))
                if not minimum <= brightness <= maximum: return False
            return True
        rows = [row for row in rows if keep(row)]
        if filters.get('color'):
            rows = Catalog.filter_colors(rows, [filters['color']], filters.get('tolerance', 30),
                                         max(.001, filters.get('coverage', .1)))
        return rows

    @staticmethod
    def filter_colors(rows, colors, tolerance=38, minimum=.08):
        if not colors:
            return rows
        palettes, owners = [], []
        for index, row in enumerate(rows):
            palette = row['feature'].get('palette', []) if row['feature'] else []
            palettes.extend(palette)
            owners.extend([index] * len(palette))
        if not palettes:
            return []
        table = np.asarray(palettes, dtype=np.float32)
        lab = rgb_lab(table[:, :3])
        accepted = np.zeros(len(rows), dtype=bool)
        for color in colors:
            target = rgb_lab([int(color[i:i + 2], 16) for i in (1, 3, 5)])
            mask = np.linalg.norm(lab - target, axis=1) <= tolerance
            scores = np.bincount(owners, weights=table[:, 3] * mask, minlength=len(rows))
            accepted |= scores >= minimum
        return [row for row, keep in zip(rows, accepted) if keep]

    @staticmethod
    def recommend(item, candidates, count=24):
        if not item or not item['feature']:
            return []
        ranked = [(similarity(item['feature'], row['feature']), row)
                  for row in candidates if row['digest'] != item['digest'] and row['feature']]
        ranked.sort(key=lambda pair: (-pair[0], pair[1]['digest']))
        return [row for _, row in ranked[:count]]
