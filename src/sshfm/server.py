"""SSH TUI and command interface for a SQLite virtual filesystem."""

import argparse
import asyncio
import json
import logging
from pathlib import Path
import shlex
import signal
import sqlite3

import asyncssh

from .store import Store, StoreError
from .service import Service
from .tui import TUI
from .access import Access, Output, Process
from .config import Config, ConfigError


HELP = """sshfm Python/SQLite. All paths are relative to virtual root.
ls [PATH]                     List entries
mkdir PATH                    Create a directory
write PATH TEXT               Create a UTF-8 text file (quote spaces)
read PATH                     Read content, stable ID and revision
save ID REVISION TEXT         Save only if the revision still matches
mv SOURCE DESTINATION         Move/rename; destination is the complete new path
rm [-r] PATH                  Remove an entry; -r includes descendants
stat PATH                     Show metadata
help                          Show commands
exit                          Close the session
"""


async def execute(service, line, owner, actor=''):
    store = service.store
    args = shlex.split(line)
    if not args:
        return None
    command, *args = args
    if command == 'help' and not args:
        return {'help': HELP}
    if command == 'ls' and len(args) <= 1:
        return await service.read(store.ls, args[0] if args else '/')
    if command == 'mkdir' and len(args) == 1:
        return await service.create(args[0], 'dir', actor=actor)
    if command == 'write' and len(args) == 2:
        return await service.create(args[0], 'file', args[1], actor)
    if command == 'read' and len(args) == 1:
        return await service.read(store.read, args[0])
    if command == 'save' and len(args) == 3:
        return await service.save(int(args[0]), int(args[1]), args[2], owner, actor)
    if command == 'mv' and len(args) == 2:
        return await service.move(*args, actor=actor)
    if command == 'rm' and len(args) == 1:
        return await service.remove(args[0])
    if command == 'rm' and len(args) == 2 and args[0] == '-r':
        return await service.remove(args[1], recursive=True)
    if command == 'stat' and len(args) == 1:
        return await service.read(store.stat, args[0])
    raise StoreError("unknown command or wrong arguments; use help")


class Session(asyncssh.SSHServerProcess):
    def __init__(self, server):
        super().__init__(lambda process: handle_client(process, server.service,
                                                      server.timezone, server.access),
                         sftp_factory=None, sftp_version=3, allow_scp=False)
        self.server = server
        self.released = False

    def connection_lost(self, exc):
        try:
            super().connection_lost(exc)
        finally:
            if not self.released:
                self.server.access.release(self.server.ip, session=True)
                self.released = True


class Server(asyncssh.SSHServer):
    def __init__(self, access, service, timezone):
        self.access, self.service, self.timezone = access, service, timezone
        self.admitted = False

    def connection_made(self, connection):
        self.ip = connection.get_extra_info('peername')[0]
        self.admitted = self.access.acquire(self.ip)
        if not self.admitted:
            logging.info('Rejected SSH connection from %s: blacklist or connection limit', self.ip)
            connection.abort()

    def connection_lost(self, exc):
        if self.admitted:
            self.access.release(self.ip)
            self.admitted = False

    def session_requested(self):
        if not self.admitted or not self.access.acquire(self.ip, session=True):
            raise asyncssh.ChannelOpenError(asyncssh.OPEN_ADMINISTRATIVELY_PROHIBITED,
                                           'IP session limit reached')
        return Session(self)

    def begin_auth(self, username):
        # Local prototype: deliberately accepts any username, as the C++ version does.
        return False


async def handle_client(process, service, timezone, access):
    peer = process.get_extra_info('peername')
    if peer is None:
        return
    output = Output(access.budget(peer[0]))
    limited = Process(process, output)

    async def serve():
        await run_client(limited, service, timezone, access.config)
        await output.drain()
        process.exit(limited.exit_status)

    handler = asyncio.create_task(serve())
    closed = asyncio.create_task(process.wait_closed())
    try:
        done, _ = await asyncio.wait([handler, closed], return_when=asyncio.FIRST_COMPLETED)
        if handler in done:
            await handler
    except (asyncssh.ConnectionLost, BrokenPipeError):
        pass
    finally:
        handler.cancel()
        await asyncio.gather(handler, return_exceptions=True)
        await output.close()
        closed.cancel()
        await asyncio.gather(closed, return_exceptions=True)


async def run_client(process, service, timezone=0, config=None):
    owner = str(id(process))
    actor = process.get_extra_info('peername')[0]
    async def run(line):
        try:
            result = await execute(service, line, owner, actor)
            if result is not None:
                process.stdout.write(json.dumps(result, ensure_ascii=False) + '\n')
            return 0
        except (StoreError, ValueError) as exc:
            process.stderr.write(json.dumps({'error': str(exc)}) + '\n')
            return 1
        except sqlite3.Error:
            logging.exception("Database operation failed")
            process.stderr.write('{"error": "database operation failed; retry"}\n')
            return 1

    try:
        if process.command is not None:
            process.exit(await run(process.command))
            return
        if process.get_terminal_type() is not None:
            await TUI(process, service, timezone, config).run()
            process.exit(0)
            return
        process.stdout.write(HELP)
        process.stdout.write('sshfm> ')
        await process.stdout.drain()
        async for line in process.stdin:
            if line.strip() == 'exit':
                break
            await run(line)
            process.stdout.write('sshfm> ')
            await process.stdout.drain()
        process.exit(0)
    except (asyncssh.ConnectionLost, asyncssh.BreakReceived, BrokenPipeError):
        pass
    finally:
        service.disconnect(owner)


async def main(args):
    path = args.config or str(Path(args.database or '/data/sshfm.sqlite3').parent / 'config.yaml')
    config = Config(path)
    settings = config.load(create=args.config is None)
    for name in ('database', 'host_key', 'host', 'port', 'time'):
        if getattr(args, name) is None:
            setattr(args, name, getattr(settings, name))
    access = Access(config)
    database = Path(args.database)
    database.parent.mkdir(parents=True, exist_ok=True)
    store = Store(database)
    service = Service(store)
    key_path = Path(args.host_key)
    key_path.parent.mkdir(parents=True, exist_ok=True)
    if not key_path.exists():
        key = asyncssh.generate_private_key('ssh-ed25519')
        with key_path.open('xb') as stream:
            key_path.chmod(0o600)
            stream.write(key.export_private_key())
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    watcher = asyncio.create_task(config.watch())
    try:
        async with await asyncssh.create_server(
                lambda: Server(access, service, args.time), args.host, args.port,
                server_host_keys=[str(key_path)], encoding='utf-8', login_timeout=15,
                line_editor=False, keepalive_interval=10, keepalive_count_max=3):
            logging.info("sshfm listening on %s:%s; config: %s", args.host, args.port, config.path)
            await stop.wait()
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)


def cli():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', help='config.yaml path (default: next to the database)')
    parser.add_argument('--database')
    parser.add_argument('--host-key')
    parser.add_argument('--host')
    parser.add_argument('--port', type=int)
    parser.add_argument('--time', '-time', type=int, help='UTC offset in hours for TUI timestamps')
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    try:
        asyncio.run(main(parser.parse_args()))
    except (ConfigError, OSError) as exc:
        parser.error(str(exc))
