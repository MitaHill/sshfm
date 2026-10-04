"""SQLite-backed virtual filesystem. Paths never address host files."""

from contextlib import closing, contextmanager
import sqlite3
import time
import zlib


class StoreError(Exception):
    pass


class Store:
    def __init__(self, database):
        self.database = str(database)
        with closing(sqlite3.connect(self.database)) as db:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    parent_id INTEGER REFERENCES entries(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK(kind IN ('dir', 'file')),
                    content BLOB,
                    codec TEXT NOT NULL DEFAULT 'zlib',
                    raw_size INTEGER NOT NULL DEFAULT 0,
                    creator_ip TEXT NOT NULL DEFAULT '',
                    editor_ip TEXT NOT NULL DEFAULT '',
                    revision INTEGER NOT NULL DEFAULT 1,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    UNIQUE(parent_id, name),
                    CHECK((kind = 'dir' AND content IS NULL) OR
                          (kind = 'file' AND content IS NOT NULL)),
                    CHECK((id = 1 AND parent_id IS NULL AND name = '' AND kind = 'dir') OR
                          (id != 1 AND parent_id IS NOT NULL AND name != '' AND
                           name NOT IN ('.', '..') AND instr(name, '/') = 0))
                );
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(entries)")}
            # Upgrade the initial TEXT prototype in one transaction, without changing IDs.
            db.execute("BEGIN IMMEDIATE")
            for name, declaration in [('codec', "TEXT NOT NULL DEFAULT 'plain'"),
                                      ('raw_size', 'INTEGER NOT NULL DEFAULT 0'),
                                      ('creator_ip', "TEXT NOT NULL DEFAULT ''"),
                                      ('editor_ip', "TEXT NOT NULL DEFAULT ''")]:
                if name not in columns:
                    db.execute(f"ALTER TABLE entries ADD COLUMN {name} {declaration}")
            for entry_id, content in db.execute(
                    "SELECT id, content FROM entries WHERE kind = 'file' AND codec = 'plain'").fetchall():
                raw = content.encode('utf-8') if isinstance(content, str) else bytes(content)
                db.execute("UPDATE entries SET content = ?, codec = 'zlib', raw_size = ? WHERE id = ?",
                           (zlib.compress(raw), len(raw), entry_id))
            now = time.time_ns()
            db.execute("INSERT OR IGNORE INTO entries "
                       "(id, parent_id, name, kind, created_at, updated_at) "
                       "VALUES (1, NULL, '', 'dir', ?, ?)", (now, now))
            db.commit()

    @contextmanager
    def connect(self, write=False):
        # Each operation owns a connection and transaction, including path resolution.
        # BEGIN IMMEDIATE serializes writers before checking revisions/relationships.
        db = sqlite3.connect(self.database, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            db.commit()
        except sqlite3.IntegrityError as exc:
            db.rollback()
            raise StoreError("name already exists or invalid relationship") from exc
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def parts(path):
        if not isinstance(path, str) or any(ord(c) < 32 or ord(c) == 127 for c in path):
            raise StoreError("invalid path")
        parts = []
        for part in path.split('/'):
            if part in ('', '.'):
                continue
            if part == '..':
                if not parts:
                    raise StoreError("path escapes root")
                parts.pop()
            else:
                parts.append(part)
        return parts

    @staticmethod
    def resolve(db, parts):
        row = db.execute("SELECT * FROM entries WHERE id = 1").fetchone()
        for part in parts:
            if row['kind'] != 'dir':
                raise StoreError("parent is not a directory")
            row = db.execute("SELECT * FROM entries WHERE parent_id = ? AND name = ?",
                             (row['id'], part)).fetchone()
            if row is None:
                raise StoreError("path not found")
        return row

    def parent(self, db, parts):
        if not parts:
            raise StoreError("root cannot be modified")
        row = self.resolve(db, parts[:-1])
        if row['kind'] != 'dir':
            raise StoreError("parent is not a directory")
        return row['id'], parts[-1]

    @staticmethod
    def info(row, include_content=False):
        result = dict(row)
        content = result.pop('content')
        result['size'] = result.pop('raw_size')
        codec = result.pop('codec')
        if include_content:
            result['content'] = (zlib.decompress(content).decode('utf-8')
                                 if content is not None and codec == 'zlib' else content)
        return result

    def stat(self, path):
        with self.connect() as db:
            return self.info(self.resolve(db, self.parts(path)))

    def ls(self, path='/'):
        with self.connect() as db:
            row = self.resolve(db, self.parts(path))
            if row['kind'] != 'dir':
                raise StoreError("not a directory")
            return [self.info(child) for child in db.execute(
                "SELECT * FROM entries WHERE parent_id = ? ORDER BY kind, name", (row['id'],))]

    def create(self, path, kind, content=None, actor=''):
        with self.connect(write=True) as db:
            parent, name = self.parent(db, self.parts(path))
            now = time.time_ns()
            cursor = db.execute("INSERT INTO entries "
                                "(parent_id, name, kind, content, codec, raw_size, creator_ip, editor_ip, created_at, updated_at) "
                                "VALUES (?, ?, ?, ?, 'zlib', ?, ?, ?, ?, ?)",
                                (parent, name, kind, zlib.compress(content.encode('utf-8'))
                                 if content is not None else None,
                                 len(content.encode('utf-8')) if content is not None else 0,
                                 actor, actor, now, now))
            return self.info(db.execute("SELECT * FROM entries WHERE id = ?",
                                       (cursor.lastrowid,)).fetchone())

    def read(self, path):
        with self.connect() as db:
            row = self.resolve(db, self.parts(path))
            if row['kind'] != 'file':
                raise StoreError("not a file")
            return self.info(row, include_content=True)

    def save(self, entry_id, revision, content, actor=''):
        # Stable identity prevents a stale path from overwriting a replacement file.
        with self.connect(write=True) as db:
            row = db.execute("SELECT * FROM entries WHERE id = ?", (entry_id,)).fetchone()
            if row is None or row['kind'] != 'file':
                raise StoreError("file no longer exists")
            if row['revision'] != revision:
                raise StoreError("save conflict: read the latest revision and retry")
            db.execute("UPDATE entries SET content = ?, codec = 'zlib', raw_size = ?, revision = revision + 1, "
                       "updated_at = ?, editor_ip = ? WHERE id = ?",
                       (zlib.compress(content.encode('utf-8')), len(content.encode('utf-8')),
                        time.time_ns(), actor, entry_id))
            return self.info(db.execute("SELECT * FROM entries WHERE id = ?",
                                       (entry_id,)).fetchone())

    def move(self, source, destination, expected_id=None, actor=''):
        with self.connect(write=True) as db:
            parts = self.parts(source)
            if not parts:
                raise StoreError("root cannot be modified")
            row = self.resolve(db, parts)
            if expected_id is not None and row['id'] != expected_id:
                raise StoreError('selected entry changed; cancel and retry')
            parent, name = self.parent(db, self.parts(destination))
            ancestor = parent
            while ancestor is not None:
                if ancestor == row['id']:
                    raise StoreError("cannot move a directory into itself")
                ancestor = db.execute("SELECT parent_id FROM entries WHERE id = ?",
                                      (ancestor,)).fetchone()['parent_id']
            db.execute("UPDATE entries SET parent_id = ?, name = ?, updated_at = ?, editor_ip = ? WHERE id = ?",
                       (parent, name, time.time_ns(), actor, row['id']))
            return self.info(db.execute("SELECT * FROM entries WHERE id = ?",
                                       (row['id'],)).fetchone())

    def remove(self, path, recursive=False, protected=(), expected_id=None):
        with self.connect(write=True) as db:
            parts = self.parts(path)
            if not parts:
                raise StoreError("root cannot be modified")
            row = self.resolve(db, parts)
            if expected_id is not None and row['id'] != expected_id:
                raise StoreError('selected entry changed; cancel and retry')
            if not recursive and db.execute("SELECT 1 FROM entries WHERE parent_id = ?",
                                            (row['id'],)).fetchone():
                raise StoreError("directory is not empty; use rm -r")
            tree = db.execute("""WITH RECURSIVE tree AS (
                SELECT id, parent_id FROM entries WHERE id = ?
                UNION ALL SELECT e.id, e.parent_id FROM entries e
                JOIN tree t ON e.parent_id = t.id
            ) SELECT * FROM tree""", (row['id'],)).fetchall()
            parents = {item['id']: item['parent_id'] for item in tree}
            locked = set(protected).intersection(parents)
            if row['id'] in locked:
                raise StoreError("file is being edited by another session")
            if locked:
                # Keep locked files and their ancestor directories; delete the rest.
                keep = set()
                for entry_id in locked:
                    while entry_id in parents and entry_id not in keep:
                        keep.add(entry_id)
                        entry_id = parents[entry_id]
                db.executemany("DELETE FROM entries WHERE id = ?",
                               [(entry_id,) for entry_id in parents if entry_id not in keep])
                return {'deleted_id': row['id'], 'partial': True}
            db.execute("DELETE FROM entries WHERE id = ?", (row['id'],))
            return {'deleted_id': row['id']}

    @staticmethod
    def path_for(db, entry_id):
        parts = []
        while entry_id != 1:
            row = db.execute("SELECT parent_id, name FROM entries WHERE id = ?",
                             (entry_id,)).fetchone()
            if row is None:
                raise StoreError("entry no longer exists")
            parts.append(row['name'])
            entry_id = row['parent_id']
        return '/' + '/'.join(reversed(parts))

    def read_id(self, entry_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM entries WHERE id = ?", (entry_id,)).fetchone()
            if row is None or row['kind'] != 'file':
                raise StoreError("file no longer exists")
            result = self.info(row, include_content=True)
            result['path'] = self.path_for(db, entry_id)
            return result

    def view(self, cwd_id, file_id=None):
        """Resolve the browser and open editor together in one read snapshot."""
        with self.connect() as db:
            row = db.execute("SELECT * FROM entries WHERE id = ?", (cwd_id,)).fetchone()
            if row is None or row['kind'] != 'dir':
                cwd_id = 1
            result = {
                'cwd_id': cwd_id,
                'path': self.path_for(db, cwd_id),
                'entries': [self.info(child) for child in db.execute(
                    "SELECT * FROM entries WHERE parent_id = ?", (cwd_id,))],
                'file': None,
            }
            if file_id is not None:
                row = db.execute("SELECT * FROM entries WHERE id = ?", (file_id,)).fetchone()
                if row is not None:
                    result['file'] = self.info(row, include_content=True)
                    result['file']['path'] = self.path_for(db, file_id)
            return result
