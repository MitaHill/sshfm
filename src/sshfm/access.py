"""Per-IP connection admission and paced SSH application output."""

import asyncio
from collections import deque
import ipaddress
import time

from .config import address


class Access:
    def __init__(self, config):
        self.config = config
        self.connections = {}
        self.sessions = {}
        self.budgets = {}

    def blocked(self, ip):
        candidates = [ip]
        if isinstance(ip, ipaddress.IPv4Address):
            candidates.append(ipaddress.IPv6Address('::ffff:' + str(ip)))
        return any(candidate in network for candidate in candidates
                   for network in self.config.settings.blacklist)

    def acquire(self, peer, session=False):
        ip = address(peer)
        counts = self.sessions if session else self.connections
        maximum = self.config.settings.max_connections_per_ip
        # Blacklist updates only affect new transports, not an existing connection.
        if (not session and self.blocked(ip)) or (maximum and counts.get(ip, 0) >= maximum):
            return False
        counts[ip] = counts.get(ip, 0) + 1
        return True

    def release(self, peer, session=False):
        ip = address(peer)
        counts = self.sessions if session else self.connections
        count = counts.get(ip, 0)
        if count > 1:
            counts[ip] = count - 1
        else:
            counts.pop(ip, None)
        if ip not in self.connections and ip not in self.sessions:
            self.budgets.pop(ip, None)

    def budget(self, peer):
        ip = address(peer)
        if ip not in self.budgets:
            self.budgets[ip] = Budget(self.config)
        return self.budgets[ip]


class Budget:
    def __init__(self, config):
        self.config = config
        self.lock = asyncio.Lock()

    async def send(self, data, writer):
        # FIFO lock acquisition rotates small chunks among active sessions.
        async with self.lock:
            rate = self.config.settings.send_rate_per_ip
            size = min(4096, max(4, rate // 10)) if rate else 4096
            text = bytes(data[:size]).decode('utf-8', errors='ignore')
            sent = len(text.encode('utf-8'))
            remaining = sent
            while remaining > 0:
                rate = self.config.settings.send_rate_per_ip
                if not rate:
                    break
                start = time.monotonic()
                await asyncio.sleep(min(.05, remaining / rate))
                remaining -= (time.monotonic() - start) * rate
            writer.write(text)
        # A client which stops reading must not hold up other clients on its IP.
        await writer.drain()
        return sent


class Output:
    def __init__(self, budget):
        self.budget = budget
        self.queue = deque()
        self.task = None
        self.writers = set()

    @property
    def ready(self):
        return not self.queue and (self.task is None or self.task.done())

    @property
    def limited(self):
        return bool(self.budget.config.settings.send_rate_per_ip)

    def write(self, writer, text):
        if not text:
            return
        if self.task is not None and self.task.done():
            self.task.result()
        self.writers.add(writer)
        if not self.limited and self.ready:
            writer.write(text)
            return
        self.queue.append((writer, text.encode('utf-8')))
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.send())

    async def send(self):
        while self.queue:
            writer, data = self.queue[0]
            data = memoryview(data)
            offset = 0
            while offset < len(data):
                offset += await self.budget.send(data[offset:], writer)
            self.queue.popleft()

    async def drain(self):
        if self.task is not None:
            await self.task
        for writer in self.writers:
            await writer.drain()

    async def close(self):
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.queue.clear()


class Writer:
    def __init__(self, writer, output):
        self.writer = writer
        self.output = output

    @property
    def ready(self):
        return self.output.ready

    @property
    def limited(self):
        return self.output.limited

    def write(self, text):
        self.output.write(self.writer, text)

    async def drain(self):
        await self.output.drain()

    def is_closing(self):
        return self.writer.is_closing()


class Process:
    def __init__(self, process, output):
        self.process = process
        self.stdout = Writer(process.stdout, output)
        self.stderr = Writer(process.stderr, output)
        self.exit_status = 0

    def __getattr__(self, name):
        return getattr(self.process, name)

    def exit(self, status):
        # SSH exit/EOF must follow the queued stdout and stderr, not discard them.
        self.exit_status = status
