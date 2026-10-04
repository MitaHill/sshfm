"""Single-process session coordination shared by the TUI and SSH commands."""

import asyncio

from .store import StoreError


class Service:
    def __init__(self, store):
        self.store = store
        self.gate = asyncio.Lock()
        self.locks = {}  # stable file ID -> session ID; viewing never takes a lock
        self.sessions = {}
        self.version = 0

    async def call(self, method, *args):
        # A cancelled SSH handler must wait for its worker before releasing the gate.
        # Otherwise disconnect could let deletion race a write still in progress.
        task = asyncio.create_task(asyncio.to_thread(method, *args))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            try:
                await task
            finally:
                self.version += 1
            raise

    async def read(self, method, *args):
        async with self.gate:
            return await self.call(method, *args)

    async def create(self, path, kind, content=None, actor=''):
        async with self.gate:
            result = await self.call(self.store.create, path, kind, content, actor)
            self.version += 1
            return result

    async def begin_edit(self, entry_id, owner):
        async with self.gate:
            if entry_id in self.locks and self.locks[entry_id] != owner:
                raise StoreError("file is being edited by another session")
            # Reload after acquiring the gate: a previous writer may just have saved.
            result = await self.call(self.store.read_id, entry_id)
            self.locks[entry_id] = owner
            self.version += 1
            return result

    async def save(self, entry_id, revision, content, owner, actor=''):
        async with self.gate:
            if entry_id in self.locks and self.locks[entry_id] != owner:
                raise StoreError("file is being edited by another session")
            result = await self.call(self.store.save, entry_id, revision, content, actor)
            self.release(entry_id, owner)
            self.version += 1
            return result

    async def move(self, source, destination, expected_id=None, actor=''):
        async with self.gate:
            result = await self.call(self.store.move, source, destination, expected_id, actor)
            self.version += 1
            return result

    async def remove(self, path, recursive=False, expected_id=None):
        async with self.gate:
            result = await self.call(self.store.remove, path, recursive, tuple(self.locks), expected_id)
            self.version += 1
            return result

    def release(self, entry_id, owner):
        if self.locks.get(entry_id) == owner:
            del self.locks[entry_id]
            self.version += 1

    def register(self, owner, ui):
        self.sessions[owner] = ui
        self.version += 1

    def disconnect(self, owner):
        for entry_id in list(self.locks):
            self.release(entry_id, owner)
        self.sessions.pop(owner, None)
        self.version += 1

    def broadcast(self, owner, text):
        sender = self.sessions[owner].ip
        for ui in self.sessions.values():
            ui.status = f'[{sender}] {text}'
            ui.redraw = True

    def bell(self, cwd_id=None):
        for ui in self.sessions.values():
            if cwd_id is None or ui.cwd_id == cwd_id:
                ui.process.stdout.write('\a')
