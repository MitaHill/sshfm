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


class LocalTestServer(asyncssh.SSHServer):
    def begin_auth(self, username):
        # Local prototype: deliberately accepts any username, as the C++ version does.
        return False


async def handle_client(process, service, timezone=0):
    owner = str(id(process))
    async def run(line):
        try:
            result = await execute(service, line, owner, process.get_extra_info('peername')[0])
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
            await TUI(process, service, timezone).run()
            process.exit(0)
            return
        process.stdout.write(HELP)
        process.stdout.write('sshfm> ')
        async for line in process.stdin:
            if line.strip() == 'exit':
                break
            await run(line)
            process.stdout.write('sshfm> ')
        process.exit(0)
    except (asyncssh.ConnectionLost, asyncssh.BreakReceived, BrokenPipeError):
        pass
    finally:
        service.disconnect(owner)


async def main(args):
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
    async with await asyncssh.create_server(
            LocalTestServer, args.host, args.port, server_host_keys=[str(key_path)],
            process_factory=lambda process: handle_client(process, service, args.time),
            encoding='utf-8', login_timeout=15, line_editor=False,
            keepalive_interval=10, keepalive_count_max=3):
        logging.info("sshfm listening on %s:%s", args.host, args.port)
        await stop.wait()


def cli():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', default='/data/sshfm.sqlite3')
    parser.add_argument('--host-key', default='/data/sshfm_hostkey')
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=2222)
    parser.add_argument('--time', '-time', type=int, default=0, help='UTC offset in hours for TUI timestamps')
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    asyncio.run(main(parser.parse_args()))
