import asyncio
from pathlib import Path
import tempfile
import time
import unittest
from types import SimpleNamespace

import asyncssh

from sshfm.access import Access, Budget, Output, Writer
from sshfm.config import Config, ConfigError, Settings, address
from sshfm.server import Server
from sshfm.service import Service
from sshfm.store import Store


class ConfigTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'config.yaml'
        self.config = Config(self.path)
        self.config.load(create=True)

    def test_template_matches_defaults_and_relative_paths(self):
        template = Path(__file__).resolve().parents[1] / 'config.yaml'
        self.assertEqual(Settings.parse(template.read_bytes(), self.path.parent), self.config.settings)
        self.assertEqual(self.config.settings.database, str((self.path.parent / 'sshfm.sqlite3').resolve()))
        self.assertEqual(self.config.settings.send_rate_per_ip, 2048)

    def test_invalid_values_are_rejected(self):
        for text in ('[]', 'blacklist: [123]', 'blacklist: [not-an-ip]', 'unknown: true',
                     'send_rate_per_ip: -1', 'send_rate_per_ip: true',
                     'max_connections_per_ip: 1.5', 'port: 65536', 'time: 24',
                     'port: 2222\nport: 2223', 'database: null'):
            with self.subTest(text=text), self.assertRaises(ConfigError):
                Settings.parse(text, self.path.parent)

    async def test_bad_reload_and_missing_file_retain_rules_then_recover(self):
        self.path.write_text('blacklist: [192.0.2.0/24, "2001:db8::/32"]\nsend_rate_per_ip: 1KB/s\n')
        self.assertTrue(await self.config.reload())
        previous = self.config.settings
        self.path.write_text('blacklist: [broken')
        with self.assertLogs(level='ERROR'):
            self.assertFalse(await self.config.reload())
        self.assertIs(self.config.settings, previous)
        self.path.unlink()
        with self.assertLogs(level='ERROR'):
            self.assertFalse(await self.config.reload())
        self.assertIs(self.config.settings, previous)
        self.path.write_text('blacklist: []\nsend_rate_per_ip: 2KiB\n')
        self.assertTrue(await self.config.reload())
        self.assertEqual(self.config.settings.send_rate_per_ip, 2048)

    def test_ip_normalization_and_admission_recovery(self):
        self.path.write_text('blacklist: [192.0.2.0/24, "2001:db8::/32", "::ffff:198.51.100.0/120"]\n')
        self.config.load()
        access = Access(self.config)
        for ip in ('192.0.2.1', '::ffff:192.0.2.1', '2001:db8::1', '198.51.100.2'):
            self.assertFalse(access.acquire(ip))
        self.assertEqual(address('fe80::1%en0'), address('fe80::1'))
        for _ in range(3):
            self.assertTrue(access.acquire('127.0.0.1'))
        self.assertFalse(access.acquire('::ffff:127.0.0.1'))
        self.assertTrue(access.acquire('127.0.0.2'))
        access.release('127.0.0.1')
        self.assertTrue(access.acquire('127.0.0.1'))
        self.assertIs(access.budget('127.0.0.1'), access.budget('::ffff:127.0.0.1'))
        self.assertIsNot(access.budget('127.0.0.1'), access.budget('127.0.0.2'))


class OutputTests(unittest.IsolatedAsyncioTestCase):
    async def test_utf8_stdout_stderr_share_budget_and_live_rate_changes(self):
        settings = SimpleNamespace(send_rate_per_ip=1024)
        budget = Budget(SimpleNamespace(settings=settings))
        chunks = []

        class Stream:
            def write(self, text):
                chunks.append((time.monotonic(), text))

            async def drain(self):
                pass

        output = Output(budget)
        stdout, stderr = Writer(Stream(), output), Writer(Stream(), output)
        text = '中👩‍💻é' * 100
        start = time.monotonic()
        stdout.write(text)
        stderr.write('error')
        self.assertFalse(stdout.ready)
        await output.drain()
        self.assertEqual(''.join(chunk for _, chunk in chunks), text + 'error')
        self.assertGreaterEqual(time.monotonic() - start, len((text + 'error').encode()) / 1024 * .95)
        self.assertTrue(stdout.ready)
        settings.send_rate_per_ip = 1
        stdout.write('next')
        await asyncio.sleep(.1)
        settings.send_rate_per_ip = 0
        await asyncio.wait_for(output.drain(), .3)
        self.assertEqual(chunks[-1][1], 'next')
        await output.close()


class SSHAccessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'config.yaml'
        self.path.write_text('send_rate_per_ip: 0\nmax_connections_per_ip: 3\n')
        self.config = Config(self.path)
        self.config.load()
        self.access = Access(self.config)
        self.store = Store(Path(self.temp.name) / 'test.sqlite3')
        self.service = Service(self.store)
        self.server = await asyncssh.create_server(
            lambda: Server(self.access, self.service, 0), '127.0.0.1', 0,
            server_host_keys=[asyncssh.generate_private_key('ssh-ed25519')],
            encoding='utf-8', line_editor=False, login_timeout=2)
        self.connections = []
        self.watcher = asyncio.create_task(self.config.watch())

    async def asyncTearDown(self):
        for connection in self.connections:
            connection.abort()
        await asyncio.gather(*(connection.wait_closed() for connection in self.connections))
        self.server.close()
        await self.server.wait_closed()
        self.watcher.cancel()
        await asyncio.gather(self.watcher, return_exceptions=True)
        self.temp.cleanup()

    async def connect(self):
        connection = await asyncssh.connect('127.0.0.1', self.server.get_port(),
                                           username='test', known_hosts=None, config=None)
        self.connections.append(connection)
        return connection

    async def reload(self, text):
        temporary = self.path.with_suffix('.tmp')
        temporary.write_text(text)
        temporary.replace(self.path)
        deadline = time.monotonic() + 3
        while self.config.raw != text.encode():
            self.assertLess(time.monotonic(), deadline, 'configuration watcher did not reload')
            await asyncio.sleep(.05)

    async def test_connection_limit_and_release(self):
        first = await self.connect()
        await self.connect()
        await self.connect()
        with self.assertRaises((asyncssh.Error, OSError)):
            await self.connect()
        self.assertEqual((await first.run('ls /')).exit_status, 0)
        first.close()
        await first.wait_closed()
        await asyncio.sleep(.05)
        replacement = await self.connect()
        self.assertEqual((await replacement.run('ls /')).exit_status, 0)

    async def test_multiplexed_sessions_cannot_bypass_limit(self):
        connection = await self.connect()
        sessions = [await connection.create_process() for _ in range(3)]
        with self.assertRaises(asyncssh.ChannelOpenError):
            await connection.create_process()
        sessions[0].close()
        await sessions[0].wait_closed()
        await asyncio.sleep(.05)
        self.assertEqual((await connection.run('ls /')).exit_status, 0)

    async def test_blacklist_reload_keeps_existing_connection_and_invalid_rules(self):
        existing = await self.connect()
        await self.reload('send_rate_per_ip: 0\nblacklist: [127.0.0.0/8]\n')
        with self.assertRaises((asyncssh.Error, OSError)):
            await self.connect()
        self.assertEqual((await existing.run('ls /')).stdout, '[]\n')
        self.path.write_text('blacklist: [invalid]')
        with self.assertLogs(level='ERROR'):
            await asyncio.sleep(1.2)
        with self.assertRaises((asyncssh.Error, OSError)):
            await self.connect()
        await self.reload('send_rate_per_ip: 0\nblacklist: []\nmax_connections_per_ip: 1\n')
        with self.assertRaises((asyncssh.Error, OSError)):
            await self.connect()
        await self.reload('send_rate_per_ip: 0\nblacklist: []\nmax_connections_per_ip: 3\n')
        self.assertEqual((await (await self.connect()).run('ls /')).exit_status, 0)

    async def test_two_connections_share_rate_and_both_make_progress(self):
        self.store.create('/large', 'file', 'x' * 2048)
        alice, bob = await self.connect(), await self.connect()
        await self.reload('send_rate_per_ip: 1KB\nmax_connections_per_ip: 3\n')
        start = time.monotonic()
        processes = await asyncio.gather(alice.create_process('read /large'),
                                         bob.create_process('read /large'))
        first = await asyncio.wait_for(asyncio.gather(*(p.stdout.read(1) for p in processes)), 1)
        self.assertEqual(first, ['{', '{'])
        results = await asyncio.gather(*(p.communicate() for p in processes))
        elapsed = time.monotonic() - start
        total = sum(len((head + result[0]).encode()) for head, result in zip(first, results))
        self.assertGreaterEqual(elapsed, total / 1024 * .95)
        for stdout, stderr in results:
            self.assertIn('x' * 2048, stdout)
            self.assertEqual(stderr, '')

    async def test_disconnect_during_slow_tui_releases_lock_and_counts(self):
        file = self.store.create('/file', 'file', 'seed')
        connection = await self.connect()
        await self.reload('send_rate_per_ip: 1\n')
        process = await connection.create_process(term_type='xterm', term_size=(300, 80))
        process.stdin.write('g/file\r\rX')
        deadline = time.monotonic() + 2
        while file['id'] not in self.service.locks:
            self.assertLess(time.monotonic(), deadline)
            await asyncio.sleep(.05)
        connection.abort()
        await connection.wait_closed()
        await asyncio.sleep(.1)
        self.assertEqual(self.service.locks, {})
        self.assertEqual(self.access.connections, {})
        self.assertEqual(self.access.sessions, {})
        self.assertEqual(self.access.budgets, {})
