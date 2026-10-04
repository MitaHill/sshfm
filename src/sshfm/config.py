"""Validated startup settings and reloadable access rules."""

import asyncio
from dataclasses import dataclass
import ipaddress
import logging
from pathlib import Path
import re

import yaml


DEFAULTS = dict(database='sshfm.sqlite3', host_key='sshfm_hostkey', host='0.0.0.0',
                port=2222, time=0, blacklist=[], send_rate_per_ip='2KB',
                max_connections_per_ip=3)
STARTUP = ('database', 'host_key', 'host', 'port', 'time')


def address(value):
    ip = ipaddress.ip_address(value.split('%', 1)[0])
    return ip.ipv4_mapped if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped else ip


class ConfigError(ValueError):
    pass


class Loader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        result = super().construct_mapping(node, deep)
        if len(result) != len(node.value):
            raise ConfigError('duplicate configuration key')
        return result


def integer(value, name, minimum=0, maximum=None):
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        raise ConfigError(f'invalid {name}')
    return value


def rate(value):
    if type(value) is int:
        return integer(value, 'send_rate_per_ip')
    if isinstance(value, str):
        match = re.fullmatch(r'(\d+)\s*(B|KB|KiB|MB|MiB)(?:/s)?', value.strip(), re.I)
        if match:
            unit = match[2].lower()
            return int(match[1]) * (1 if unit == 'b' else 1024 if unit in ('kb', 'kib') else 1024 ** 2)
    raise ConfigError('send_rate_per_ip must be bytes/second or a size such as 2KB')


@dataclass(frozen=True)
class Settings:
    database: str
    host_key: str
    host: str
    port: int
    time: int
    blacklist: tuple
    send_rate_per_ip: int
    max_connections_per_ip: int

    @classmethod
    def parse(cls, raw, directory):
        try:
            values = yaml.load(raw, Loader=Loader)
        except yaml.YAMLError as exc:
            raise ConfigError(str(exc)) from exc
        if not isinstance(values, dict):
            raise ConfigError('config.yaml must contain a mapping')
        if values.keys() - DEFAULTS.keys():
            raise ConfigError('unknown configuration key')
        values = DEFAULTS | values
        for name in ('database', 'host_key', 'host'):
            if not isinstance(values[name], str) or not values[name].strip():
                raise ConfigError(f'invalid {name}')
        for name in ('database', 'host_key'):
            values[name] = str((directory / values[name]).resolve())
        integer(values['port'], 'port', 1, 65535)
        integer(values['time'], 'time', -23, 23)
        integer(values['max_connections_per_ip'], 'max_connections_per_ip')
        if not isinstance(values['blacklist'], list):
            raise ConfigError('blacklist must be a list of IP addresses or CIDR networks')
        if any(not isinstance(item, str) for item in values['blacklist']):
            raise ConfigError('blacklist entries must be strings')
        try:
            values['blacklist'] = tuple(ipaddress.ip_network(item, strict=False)
                                        for item in values['blacklist'])
        except ValueError as exc:
            raise ConfigError('invalid blacklist address or network') from exc
        values['send_rate_per_ip'] = rate(values['send_rate_per_ip'])
        return cls(**values)


class Config:
    def __init__(self, path):
        self.path = Path(path)
        self.raw = None
        self.settings = None
        self.error = None

    def read(self):
        with self.path.open('rb') as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise ConfigError('config.yaml exceeds 64 KiB')
        return raw

    def load(self, create=False):
        if create and not self.path.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open('x', encoding='utf-8') as stream:
                yaml.safe_dump(DEFAULTS, stream, sort_keys=False)
            logging.info('Created configuration at %s', self.path)
        raw = self.read()
        self.settings = Settings.parse(raw, self.path.parent)
        self.raw = raw
        return self.settings

    async def reload(self):
        try:
            raw = await asyncio.to_thread(self.read)
            if raw == self.raw:
                self.error = None
                return False
            settings = Settings.parse(raw, self.path.parent)
        except (OSError, ValueError, TypeError) as exc:
            message = str(exc)
            if message != self.error:
                logging.error('Cannot reload %s; keeping previous rules: %s', self.path, message)
                self.error = message
            return False
        if any(getattr(settings, name) != getattr(self.settings, name) for name in STARTUP):
            logging.warning('Startup settings in %s changed; restart to apply them', self.path)
        self.settings, self.raw, self.error = settings, raw, None
        logging.info('Reloaded access and traffic rules from %s', self.path)
        return True

    async def watch(self):
        while True:
            await asyncio.sleep(1)
            await self.reload()
