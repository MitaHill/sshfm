import asyncio
from pathlib import Path
import sqlite3
import tempfile
import unittest

from sshfm.service import Service
from sshfm.store import Store, StoreError


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'test.sqlite3')
        self.service = Service(self.store)
        self.file = await self.service.create('/file', 'file', 'seed')

    async def test_readers_and_exclusive_first_change(self):
        readers = await asyncio.gather(*[
            self.service.read(self.store.read_id, self.file['id']) for _ in range(2)])
        self.assertEqual([r['content'] for r in readers], ['seed', 'seed'])
        self.assertEqual(self.service.locks, {})
        await self.service.begin_edit(self.file['id'], 'alice')
        with self.assertRaises(StoreError):
            await self.service.begin_edit(self.file['id'], 'bob')
        self.assertEqual((await self.service.read(self.store.read, '/file'))['content'], 'seed')
        with self.assertRaises(StoreError):
            await self.service.save(self.file['id'], 1, 'bypass', 'command-session')
        await self.service.save(self.file['id'], 1, 'alice saved', 'alice')
        self.assertEqual(self.service.locks, {})
        latest = await self.service.begin_edit(self.file['id'], 'bob')
        self.assertEqual(latest['content'], 'alice saved')
        self.assertEqual(latest['revision'], 2)

    async def test_save_failure_keeps_lock_and_retry_succeeds(self):
        await self.service.begin_edit(self.file['id'], 'alice')
        with sqlite3.connect(self.store.database) as db:
            db.execute("CREATE TRIGGER fail_save BEFORE UPDATE OF content ON entries "
                       "BEGIN SELECT RAISE(ABORT, 'injected save failure'); END")
        with self.assertRaises(StoreError):
            await self.service.save(self.file['id'], 1, 'draft', 'alice')
        self.assertEqual(self.service.locks[self.file['id']], 'alice')
        self.assertEqual((await self.service.read(self.store.read, '/file'))['content'], 'seed')
        with sqlite3.connect(self.store.database) as db:
            db.execute('DROP TRIGGER fail_save')
        await self.service.save(self.file['id'], 1, 'draft', 'alice')
        self.assertEqual(self.service.locks, {})

    async def test_move_lock_partial_delete_and_disconnect(self):
        await self.service.create('/dir', 'dir')
        await self.service.move('/file', '/dir/locked')
        await self.service.create('/dir/unlocked', 'file', 'remove me')
        await self.service.begin_edit(self.file['id'], 'alice')
        await self.service.move('/dir', '/renamed')
        self.assertEqual(self.service.locks[self.file['id']], 'alice')
        result = await self.service.remove('/renamed', True)
        self.assertTrue(result['partial'])
        self.assertEqual([e['name'] for e in self.store.ls('/renamed')], ['locked'])
        with self.assertRaises(StoreError):
            await self.service.remove('/renamed/locked')
        self.service.disconnect('alice')
        await self.service.remove('/renamed', True)
        self.assertEqual(self.store.ls(), [])

    async def test_cancelled_worker_finishes_before_gate_release(self):
        import threading
        started, finish = threading.Event(), threading.Event()

        def slow_save(*args):
            started.set()
            finish.wait(timeout=3)
            return self.store.save(*args)

        task = asyncio.create_task(self.service.read(slow_save, self.file['id'], 1, 'done'))
        await asyncio.to_thread(started.wait, 1)
        task.cancel()
        await asyncio.sleep(0)
        self.assertTrue(self.service.gate.locked())
        finish.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.service.gate.locked())
        self.assertEqual(self.store.read('/file')['content'], 'done')

    async def test_stale_confirmation_cannot_remove_replacement(self):
        await self.service.move('/file', '/moved')
        replacement = await self.service.create('/file', 'file', 'replacement')
        with self.assertRaises(StoreError):
            await self.service.remove('/file', True, self.file['id'])
        with self.assertRaises(StoreError):
            await self.service.move('/file', '/oops', self.file['id'])
        self.assertEqual(self.store.read('/file')['id'], replacement['id'])
        self.assertEqual(self.store.read('/moved')['id'], self.file['id'])


if __name__ == '__main__':
    unittest.main()
