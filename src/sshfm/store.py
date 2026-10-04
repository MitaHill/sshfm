"""SQLite-backed virtual filesystem. Paths never address host files."""

from contextlib import closing, contextmanager
import sqlite3
import time


# Lists and path traversal return metadata; file text is fetched only for reading.
COLUMNS = """id, parent_id, name, kind, revision, created_at, updated_at,
             creator_ip, editor_ip, coalesce(length(CAST(content AS BLOB)), 0) AS size"""


class StoreError(Exception):
    pass


class Store:
    def __init__(self, database):
        self.database = str(database)
        with closing(sqlite3.connect(self.database)) as db:
            if any(row[1] == 'codec' for row in db.execute("PRAGMA table_info(entries)")):
                raise StoreError("legacy compressed database; use an offline copy or a new database")
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    parent_id INTEGER REFERENCES entries(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK(kind IN ('dir', 'file')),
                    content TEXT,
                    creator_ip TEXT NOT NULL DEFAULT '',
                    editor_ip TEXT NOT NULL DEFAULT '',
                    revision INTEGER NOT NULL DEFAULT 1,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    UNIQUE(parent_id, name),
                    CHECK((kind = 'dir' AND content IS NULL) OR
                          (kind = 'file' AND typeof(content) = 'text')),
                    CHECK((id = 1 AND parent_id IS NULL AND name = '' AND kind = 'dir') OR
                          (id != 1 AND parent_id IS NOT NULL AND name != '' AND
                           name NOT IN ('.', '..') AND instr(name, '/') = 0))
                );
            """)
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
        row = db.execute(f"SELECT {COLUMNS} FROM entries WHERE id = 1").fetchone()
        for part in parts:
            if row['kind'] != 'dir':
                raise StoreError("parent is not a directory")
            row = db.execute(f"SELECT {COLUMNS} FROM entries WHERE parent_id = ? AND name = ?",
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

    def stat(self, path):
        with self.connect() as db:
            return dict(self.resolve(db, self.parts(path)))

    def ls(self, path='/'):
        with self.connect() as db:
            row = self.resolve(db, self.parts(path))
            if row['kind'] != 'dir':
                raise StoreError("not a directory")
            return [dict(child) for child in db.execute(
                f"SELECT {COLUMNS} FROM entries WHERE parent_id = ? ORDER BY kind, name", (row['id'],))]

    def create(self, path, kind, content=None, actor=''):
        with self.connect(write=True) as db:
            parent, name = self.parent(db, self.parts(path))
            now = time.time_ns()
            cursor = db.execute("INSERT INTO entries "
                                "(parent_id, name, kind, content, creator_ip, editor_ip, created_at, updated_at) "
                                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                                (parent, name, kind, content, actor, actor, now, now))
            return dict(db.execute(f"SELECT {COLUMNS} FROM entries WHERE id = ?",
                                   (cursor.lastrowid,)).fetchone())

    def read(self, path):
        with self.connect() as db:
            row = self.resolve(db, self.parts(path))
            if row['kind'] != 'file':
                raise StoreError("not a file")
            result = dict(row)
            result['content'] = db.execute("SELECT content FROM entries WHERE id = ?", (row['id'],)).fetchone()[0]
            return result

    def save(self, entry_id, revision, content, actor=''):
        # Stable identity prevents a stale path from overwriting a replacement file.
        with self.connect(write=True) as db:
            row = db.execute(f"SELECT {COLUMNS} FROM entries WHERE id = ?", (entry_id,)).fetchone()
            if row is None or row['kind'] != 'file':
                raise StoreError("file no longer exists")
            if row['revision'] != revision:
                raise StoreError("save conflict: read the latest revision and retry")
            db.execute("UPDATE entries SET content = ?, revision = revision + 1, "
                       "updated_at = ?, editor_ip = ? WHERE id = ?",
                       (content, time.time_ns(), actor, entry_id))
            return dict(db.execute(f"SELECT {COLUMNS} FROM entries WHERE id = ?",
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
            return dict(db.execute(f"SELECT {COLUMNS} FROM entries WHERE id = ?",
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
            row = db.execute(f"SELECT {COLUMNS}, content FROM entries WHERE id = ?", (entry_id,)).fetchone()
            if row is None or row['kind'] != 'file':
                raise StoreError("file no longer exists")
            result = dict(row)
            result['path'] = self.path_for(db, entry_id)
            return result

    def view(self, cwd_id, file_id=None):
        """Resolve the browser and open editor together in one read snapshot."""
        with self.connect() as db:
            row = db.execute(f"SELECT {COLUMNS} FROM entries WHERE id = ?", (cwd_id,)).fetchone()
            if row is None or row['kind'] != 'dir':
                cwd_id = 1
            result = {
                'cwd_id': cwd_id,
                'path': self.path_for(db, cwd_id),
                'entries': [dict(child) for child in db.execute(
                    f"SELECT {COLUMNS} FROM entries WHERE parent_id = ?", (cwd_id,))],
                'file': None,
            }
            if file_id is not None:
                row = db.execute(f"SELECT {COLUMNS}, content FROM entries WHERE id = ?", (file_id,)).fetchone()
                if row is not None:
                    result['file'] = dict(row)
                    result['file']['path'] = self.path_for(db, file_id)
            return result
