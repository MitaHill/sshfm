#!/usr/bin/env python3
"""Run isolated Docker/OpenSSH integration checks; no Python dependencies needed."""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time
import uuid

from tui_checks import run_tui_checks
from access_checks import run_access_checks


def docker(*args):
    result = subprocess.run(['docker', *args], check=True, capture_output=True,
                            text=True, timeout=60)
    return (result.stdout + (result.stderr if args[0] == 'logs' else '')).strip()


def run(image):
    name = 'sshfm-test-' + uuid.uuid4().hex[:12]
    volume = name + '-data'
    with tempfile.TemporaryDirectory(prefix='sshfm-ssh-') as temp:
        key = Path(temp) / 'client-key'
        known_hosts = Path(temp) / 'known_hosts'
        subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(key)],
                       check=True, timeout=10)
        docker('volume', 'create', volume)
        port = None

        def start():
            nonlocal port
            docker('run', '-d', '--name', name, '--init', '-p', '127.0.0.1::2222',
                   '-v', volume + ':/data', image)
            port = docker('port', name, '2222/tcp').rsplit(':', 1)[1]
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                scan = subprocess.run(['ssh-keyscan', '-T', '1', '-t', 'ed25519',
                                       '-p', port, '127.0.0.1'],
                                      capture_output=True, text=True, timeout=5)
                if scan.returncode == 0 and scan.stdout.strip():
                    keys = [line for line in scan.stdout.splitlines()
                            if line and not line.startswith('#')]
                    if keys:
                        known_hosts.write_text('\n'.join(keys) + '\n')
                        return keys[0].split()[1:3]
                if docker('inspect', '-f', '{{.State.Running}}', name) != 'true':
                    raise AssertionError(docker('logs', name))
                time.sleep(0.2)
            raise AssertionError('SSH startup timed out: ' + docker('logs', name))

        def ssh_args(tty=False):
            return ['ssh', '-F', '/dev/null', '-tt' if tty else '-T', '-p', port,
                    '-i', str(key), '-o', 'IdentityAgent=none', '-o', 'IdentitiesOnly=yes',
                    '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5',
                    '-o', 'StrictHostKeyChecking=yes', '-o', 'LogLevel=ERROR',
                    '-o', 'UserKnownHostsFile=' + str(known_hosts), 'test@127.0.0.1']

        def command(*args, ok=True):
            result = subprocess.run(ssh_args() + [shlex.join(str(arg) for arg in args)],
                                    capture_output=True, text=True, timeout=15)
            assert result.returncode == (0 if ok else 1), (args, result)
            return json.loads(result.stdout if ok else result.stderr)

        try:
            # Existing UI regression checks run without pacing; rate and admission
            # rules are exercised separately by the real-SSH tests in test_access.
            docker('run', '--rm', '-v', volume + ':/data', image, 'python', '-c',
                   "from pathlib import Path; Path('/data/config.yaml').write_text("
                   "'send_rate_per_ip: 0\\nmax_connections_per_ip: 3\\n')")
            host_key = start()
            command('mkdir', '/notes')
            initial = command('write', '/notes/你好.txt', '中文 👩‍💻\né')
            saved = command('read', '/notes/你好.txt')
            assert saved['content'] == '中文 👩‍💻\né'
            assert saved['id'] == initial['id']
            assert command('ls', '/notes')[0]['id'] == initial['id']
            print('PASS: real SSH create/list/read and UTF-8 contents', flush=True)

            # Separate SSH connections race the same revision: only one may commit.
            def save(text):
                return subprocess.run(ssh_args() + [shlex.join([
                    'save', str(initial['id']), '1', text])],
                    capture_output=True, text=True, timeout=15)

            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(save, ['alice', 'bob']))
            assert sorted(result.returncode for result in results) == [0, 1], results
            assert 'save conflict' in next(r.stderr for r in results if r.returncode == 1)
            committed = command('read', '/notes/你好.txt')
            assert committed['revision'] == 2
            assert committed['content'] in ('alice', 'bob')
            command('mv', '/notes', '/archive')
            assert command('read', '/archive/你好.txt')['id'] == initial['id']
            command('save', initial['id'], 2, 'persisted 👋')
            print('PASS: concurrent SSH saves reject stale revision; move keeps ID', flush=True)

            assert 'path escapes root' in command('mkdir', '../escape', ok=False)['error']
            command('mv', '/archive', '/archive/inside', ok=False)
            command('write', '/archive/你好.txt', 'overwrite', ok=False)
            command('rm', '/archive', ok=False)
            assert command('read', '/archive/你好.txt')['content'] == 'persisted 👋'
            print('PASS: invalid paths, move cycles and failed changes preserve data', flush=True)

            shell = subprocess.run(ssh_args(), input='ls /archive\nexit\n',
                                   capture_output=True, text=True, timeout=15)
            assert shell.returncode == 0 and 'sshfm> ' in shell.stdout, shell
            assert '你好.txt' in shell.stdout, shell.stdout
            print('PASS: non-PTY command shell', flush=True)

            run_tui_checks(ssh_args, command, lambda *args: docker('exec', name, *args))
            run_access_checks(ssh_args, command, lambda *args: docker('exec', name, *args))

            docker('stop', '-t', '5', name)
            docker('rm', name)
            assert start() == host_key, 'SSH host key changed after recreation'
            persisted = command('read', '/archive/你好.txt')
            assert persisted['id'] == initial['id']
            assert persisted['revision'] == 3 and persisted['content'] == 'persisted 👋'
            print('PASS: database and SSH host key survive container recreation', flush=True)

            command('rm', '-r', '/archive')
            assert command('ls') == []
            command('save', initial['id'], 3, 'deleted', ok=False)
            check = docker('exec', name, 'python', '-c',
                           "import sqlite3; c=sqlite3.connect('/data/sshfm.sqlite3'); "
                           "assert c.execute('PRAGMA integrity_check').fetchone()[0]=='ok'; "
                           "assert not c.execute('PRAGMA foreign_key_check').fetchall(); "
                           "print('ok')")
            assert check == 'ok'
            print('PASS: recursive deletion, stale deleted ID and SQLite integrity', flush=True)
            logs = docker('logs', name)
            assert 'Traceback' not in logs, logs
        except Exception:
            result = subprocess.run(['docker', 'logs', name], capture_output=True, text=True)
            print(result.stdout + result.stderr, file=sys.stderr)
            raise
        finally:
            subprocess.run(['docker', 'rm', '-f', name], capture_output=True, timeout=30)
            docker('volume', 'rm', volume)


if __name__ == '__main__':
    run(sys.argv[1] if len(sys.argv) > 1 else 'sshfm:python-local')
