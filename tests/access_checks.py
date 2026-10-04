"""Verify the deployed config watcher and admission rules over OpenSSH."""

import json
import os
import select
import subprocess
import time


def read_prompt(process, timeout=5):
    data = b''
    deadline = time.monotonic() + timeout
    while not data.endswith(b'sshfm> '):
        if time.monotonic() >= deadline:
            raise AssertionError(f'SSH prompt timed out: {data!r}')
        if select.select([process.stdout], [], [], .05)[0]:
            chunk = os.read(process.stdout.fileno(), 65536)
            assert chunk, f'SSH shell disconnected: {data!r}'
            data += chunk
    return data


def run_access_checks(ssh_args, command, container):
    baseline = 'send_rate_per_ip: 0\nmax_connections_per_ip: 3\n'

    def configure(text):
        container('python', '-c',
                  'import sys; from pathlib import Path; '
                  "p=Path('/data/config.tmp'); p.write_text(sys.argv[1]); "
                  "p.replace('/data/config.yaml')", text)
        time.sleep(1.3)

    shells = []
    try:
        for _ in range(3):
            process = subprocess.Popen(ssh_args(), stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            shells.append(process)
            read_prompt(process)
        rejected = subprocess.run(ssh_args() + ['ls /'], capture_output=True, timeout=5)
        assert rejected.returncode == 255, rejected
        closed = shells.pop()
        closed.terminate()
        closed.wait(timeout=5)
        for stream in (closed.stdin, closed.stdout, closed.stderr):
            stream.close()
        # Release a slot before changing admission rules.
        time.sleep(.2)
        assert isinstance(command('ls', '/'), list)
        print('PASS: deployed per-IP connection cap and recovery after disconnect', flush=True)

        configure('send_rate_per_ip: 0\nblacklist: [0.0.0.0/0, "::/0"]\n')
        rejected = subprocess.run(ssh_args() + ['ls /'], capture_output=True, timeout=5)
        assert rejected.returncode == 255, rejected
        shells[0].stdin.write(b'ls /\n')
        shells[0].stdin.flush()
        assert b'[' in read_prompt(shells[0])
        configure('blacklist: [invalid]\n')
        rejected = subprocess.run(ssh_args() + ['ls /'], capture_output=True, timeout=5)
        assert rejected.returncode == 255, rejected
        configure(baseline)
        assert isinstance(command('ls', '/'), list)
        print('PASS: deployed blacklist reload retains existing SSH shell and survives invalid YAML rules', flush=True)

        command('write', '/rate-check', '中' * 1024)
        configure('send_rate_per_ip: 1KB\nmax_connections_per_ip: 3\n')
        start = time.monotonic()
        result = subprocess.run(ssh_args() + ['read /rate-check'], capture_output=True, timeout=10)
        elapsed = time.monotonic() - start
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)['content'] == '中' * 1024
        assert elapsed >= len(result.stdout) / 1024 * .95, (elapsed, len(result.stdout))
        configure(baseline)
        command('rm', '/rate-check')
        print('PASS: deployed config limits UTF-8 SSH output to 1KB/sec without truncation', flush=True)
    finally:
        for process in shells:
            process.terminate()
            process.wait(timeout=5)
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()
        configure(baseline)
