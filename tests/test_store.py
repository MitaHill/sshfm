from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from sshfm.store import Store, StoreError


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name) / 'test.sqlite3'
        self.store = Store(self.database)

    def test_create_read_and_reopen(self):
        directory = self.store.create('/笔记', 'dir')
        file = self.store.create('/笔记/你好.txt', 'file', '中文 👩‍💻\né')
        reopened = Store(self.database)
        self.assertEqual(reopened.read('/笔记/你好.txt')['content'], '中文 👩‍💻\né')
        self.assertEqual(reopened.ls('/笔记')[0]['id'], file['id'])
        self.assertEqual(file['parent_id'], directory['id'])
        self.assertEqual(file['size'], len('中文 👩‍💻\né'.encode()))

    def test_move_keeps_identity_and_subtree(self):
        self.store.create('/a', 'dir')
        self.store.create('/a/b', 'dir')
        file = self.store.create('/a/b/file', 'file', 'before')
        self.store.move('/a', '/renamed')
        self.assertEqual(self.store.read('/renamed/b/file')['id'], file['id'])
        self.store.save(file['id'], file['revision'], 'after')
        self.assertEqual(self.store.read('/renamed/b/file')['content'], 'after')
        with self.assertRaises(StoreError):
            self.store.stat('/a')

    def test_cycle_and_duplicate_move_roll_back(self):
        self.store.create('/a', 'dir')
        self.store.create('/a/b', 'dir')
        self.store.create('/other', 'dir')
        for destination in ('/a/b/a', '/other'):
            with self.assertRaises(StoreError):
                self.store.move('/a', destination)
        self.assertEqual(self.store.stat('/a/b')['kind'], 'dir')

    def test_invalid_paths_and_file_parent(self):
        self.store.create('/file', 'file', '')
        for path in ('../escape', '/a/../../escape', '/bad\x00name', '/bad\nname',
                     '/file/child', '/'):
            with self.subTest(path=path), self.assertRaises(StoreError):
                self.store.create(path, 'dir')
        self.assertEqual(self.store.stat('/./file')['kind'], 'file')
        with self.assertRaises(StoreError):
            self.store.read('/')
        with self.assertRaises(StoreError):
            self.store.ls('/file')

    def test_recursive_delete_and_root_protection(self):
        self.store.create('/a', 'dir')
        self.store.create('/a/b', 'dir')
        file = self.store.create('/a/b/file', 'file', 'data')
        with self.assertRaises(StoreError):
            self.store.remove('/a')
        self.store.remove('/a', recursive=True)
        self.assertEqual(self.store.ls(), [])
        with self.assertRaises(StoreError):
            self.store.save(file['id'], 1, 'stale')
        for operation in (lambda: self.store.remove('/', True),
                          lambda: self.store.move('/', '/root')):
            with self.assertRaises(StoreError):
                operation()

    def test_failed_save_and_replacement_do_not_lose_data(self):
        file = self.store.create('/file', 'file', 'original')
        with self.assertRaises(StoreError):
            self.store.save(file['id'], 0, 'stale')
        self.assertEqual(self.store.read('/file')['content'], 'original')
        self.store.remove('/file')
        replacement = self.store.create('/file', 'file', 'replacement')
        self.assertNotEqual(file['id'], replacement['id'])
        with self.assertRaises(StoreError):
            self.store.save(file['id'], 1, 'stale')
        self.assertEqual(self.store.read('/file')['content'], 'replacement')

    def test_concurrent_saves_have_one_winner(self):
        file = self.store.create('/file', 'file', 'seed')
        barrier = threading.Barrier(2)

        def save(content):
            barrier.wait()
            try:
                self.store.save(file['id'], 1, content)
                return True
            except StoreError:
                return False

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(save, ['alice', 'bob']))
        self.assertEqual(sorted(results), [False, True])
        saved = self.store.read('/file')
        self.assertIn(saved['content'], ('alice', 'bob'))
        self.assertEqual(saved['revision'], 2)

    def test_wal_readers_see_committed_content(self):
        self.store.create('/file', 'file', 'before')
        db = sqlite3.connect(self.database, isolation_level=None)
        self.addCleanup(db.close)
        self.assertEqual(db.execute('PRAGMA journal_mode').fetchone()[0], 'wal')
        db.execute('BEGIN IMMEDIATE')
        db.execute("UPDATE entries SET content = 'uncommitted' WHERE name = 'file'")
        self.assertEqual(self.store.read('/file')['content'], 'before')
        db.rollback()
        self.assertEqual(self.store.read('/file')['content'], 'before')
        self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_plain_text_storage_metadata_and_utf8_size(self):
        text = '中文 👩‍💻\n\x00tail'
        file = self.store.create('/plain', 'file', text, '127.0.0.1')
        with sqlite3.connect(self.database) as db:
            content, kind = db.execute(
                "SELECT content, typeof(content) FROM entries WHERE id = ?", (file['id'],)).fetchone()
            columns = {row[1] for row in db.execute('PRAGMA table_info(entries)')}
        self.assertEqual((content, kind), (text, 'text'))
        self.assertTrue({'codec', 'raw_size'}.isdisjoint(columns))
        self.assertEqual(file['size'], len(text.encode()))
        self.assertNotIn('content', self.store.ls()[0])
        self.assertNotIn('content', self.store.view(1)['entries'][0])
        self.store.save(file['id'], 1, text + 'end', '127.0.0.2')
        saved = self.store.read('/plain')
        self.assertEqual(saved['content'], text + 'end')
        self.assertEqual(saved['size'], len((text + 'end').encode()))
        self.assertEqual(saved['editor_ip'], '127.0.0.2')
        self.assertEqual(saved['creator_ip'], '127.0.0.1')
        with self.assertRaises(StoreError):
            self.store.save(file['id'], saved['revision'], b'not text')
        self.assertEqual(self.store.read('/plain'), saved)

    def test_legacy_compressed_database_is_rejected_without_changing_it(self):
        legacy = Path(self.temp.name) / 'legacy.sqlite3'
        with sqlite3.connect(legacy) as db:
            db.execute("CREATE TABLE entries(id INTEGER PRIMARY KEY, content BLOB, codec TEXT)")
            db.execute("INSERT INTO entries VALUES(7,?, 'zlib')", (b'legacy compressed bytes',))
        original = legacy.read_bytes()
        with self.assertRaisesRegex(StoreError, 'legacy compressed database'):
            Store(legacy)
        self.assertEqual(legacy.read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
