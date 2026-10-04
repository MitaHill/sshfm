"""Exercise the real full-screen UI over OpenSSH PTYs."""

import fcntl
import os
import pty
import select
import signal
import struct
import subprocess
import termios
import time
import tty


class Client:
    def __init__(self, args):
        self.master, slave = pty.openpty()
        tty.setraw(slave)
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 24, 90, 0, 0))
        self.proc = subprocess.Popen(args(tty=True), stdin=slave, stdout=slave, stderr=slave,
                                     env=dict(os.environ, TERM='xterm-256color'),
                                     start_new_session=True)
        os.close(slave)
        self.output = b''
        self.wait('sshfm')
        self.wait('q:quit')

    def drain(self):
        while select.select([self.master], [], [], 0)[0]:
            try:
                self.output += os.read(self.master, 65536)
            except OSError:
                break

    def send(self, keys):
        self.drain()
        self.output = b''
        os.write(self.master, keys.encode() if isinstance(keys, str) else keys)

    def wait(self, text, timeout=7):
        text = text.encode() if isinstance(text, str) else text
        deadline = time.monotonic() + timeout
        while text not in self.output:
            if time.monotonic() >= deadline:
                raise AssertionError(f'TUI did not display {text!r}: {self.output[-3000:]!r}')
            if select.select([self.master], [], [], 0.05)[0]:
                try:
                    data = os.read(self.master, 65536)
                except OSError as exc:
                    raise AssertionError(f'TUI disconnected: {self.output[-1000:]!r}') from exc
                if not data:
                    raise AssertionError('SSH PTY EOF')
                self.output += data

    def resize(self, width, height):
        self.output = b''
        fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack('HHHH', height, width, 0, 0))
        os.kill(self.proc.pid, signal.SIGWINCH)

    def close(self):
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait(timeout=5)
        os.close(self.master)


def run_tui_checks(ssh_args, command, container):
    command('mkdir', '/tui')
    alice = Client(ssh_args)
    bob = None
    try:
        alice.send('g/tui/\r')
        alice.wait('sshfm  /tui')
        alice.send('ndoc\r')
        alice.wait('created doc')
        file = command('read', '/tui/doc')
        alice.send('\r')
        alice.wait('edit: tui/doc')
        bob = Client(ssh_args)
        bob.send('g/tui/doc\r')
        bob.wait('goto tui/doc')
        bob.send('\r')
        bob.wait('edit: tui/doc')
        text = '中文 👩‍💻\né flag 🇹🇼\nlast'
        alice.send('\x1b[200~' + text + '\x1b[201~')
        alice.wait('last')
        bob.send('X')
        bob.wait('file is being edited')
        assert 'being edited' in command('save', file['id'], 1, 'bypass', ok=False)['error']
        alice.send('\x13')
        alice.wait('saved')
        bob.wait('last')
        assert command('read', '/tui/doc')['content'] == text
        print('PASS: TUI create/open, multiline paste, shared viewers, first-change lock and reload', flush=True)

        # Ctrl-G uses logical lines; Backspace removes a complete flag grapheme.
        alice.send('\x07' + '2\r\x1b[F\x7f')
        alice.wait('line 2/3')
        alice.send('\x13')
        alice.wait('saved')
        text = '中文 👩‍💻\né flag \nlast'
        assert command('read', '/tui/doc')['content'] == text
        alice.send('\x07' + '1\r\x1b[C\x7f\x13')
        alice.wait('saved')
        text = '文 👩‍💻\né flag \nlast'
        assert command('read', '/tui/doc')['content'] == text
        alice.send('\x1a\x13')
        alice.wait('saved')
        text = '\x1a' + text
        assert command('read', '/tui/doc')['content'] == text
        alice.wait(b'\x1b[90m^Z\x1b[39m')
        print('PASS: upstream Ctrl-G, grapheme deletion and control-byte rendering without added shortcuts', flush=True)

        alice.send('\x1b[Fdraft')
        alice.wait('draft')
        container('python', '-c', "import sqlite3; c=sqlite3.connect('/data/sshfm.sqlite3'); "
                  "c.execute(\"CREATE TRIGGER fail_save BEFORE UPDATE OF content ON entries "
                  "BEGIN SELECT RAISE(ABORT, 'test failure'); END\"); c.commit()")
        alice.send('\x13')
        alice.wait('save failed')
        assert command('read', '/tui/doc')['content'] == text
        bob.send('Y')
        bob.wait('file is being edited')
        container('python', '-c', "import sqlite3; c=sqlite3.connect('/data/sshfm.sqlite3'); "
                  "c.execute('DROP TRIGGER fail_save'); c.commit()")
        alice.send('\x13')
        alice.wait('saved')
        text = command('read', '/tui/doc')['content']
        assert 'draft' in text
        print('PASS: real SQLite save failure retains TUI draft/lock, retry saves', flush=True)

        alice.send('unsaved\x1b')
        alice.wait("type 'esc' to discard changes: ")
        alice.send('esc\r')
        alice.wait('q:quit')
        assert command('read', '/tui/doc')['content'] == text
        bob.send('B')
        bob.wait('B')
        bob.close()
        bob = None
        deadline = time.monotonic() + 5
        while True:
            try:
                command('save', file['id'], command('read', '/tui/doc')['revision'], text)
                break
            except AssertionError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.1)
        print('PASS: TUI discard and disconnect release edit locks', flush=True)

        alice.send('\rZ')
        alice.wait('Z')
        command('mv', '/tui/doc', '/tui/moved')
        alice.wait('edit: tui/moved')
        command('write', '/tui/unlocked', 'delete me')
        result = command('rm', '-r', '/tui')
        assert result['partial'] is True
        assert [e['name'] for e in command('ls', '/tui')] == ['moved']
        alice.send('\x13')
        alice.wait('saved')
        assert command('read', '/tui/moved')['id'] == file['id']
        print('PASS: TUI locked file moves by stable ID; recursive deletion preserves locked entries', flush=True)

        alice.resize(40, 12)
        alice.wait('edit: tui/moved')
        alice.resize(12, 6)
        alice.wait('^S:save')
        alice.resize(100, 30)
        alice.wait('edit: tui/moved')
        alice.send('\x1b')
        alice.wait('q:quit')
        alice.send('d')
        alice.wait("type 'del' to confirm: ")
        alice.send('wrong\r')
        alice.wait('not deleted')
        assert command('read', '/tui/moved')['id'] == file['id']
        alice.send('Nfolder\r')
        alice.wait('mkdir folder')
        alice.send('gfolder\r')
        alice.wait('goto tui/folder')
        alice.send('mrenamed\r')
        alice.wait('moved')
        assert command('stat', '/tui/renamed')['kind'] == 'dir'
        alice.send('grenamed/\r')
        alice.wait('goto tui/renamed/')
        alice.send('nchild\r')
        alice.wait('created child')
        alice.send('ddel\r')
        alice.wait('deleted')
        assert command('ls', '/tui/renamed') == []
        alice.send('Btest broadcast\r')
        alice.wait('broadcast sent')
        alice.send('b')
        alice.wait(b'\a')
        alice.send('\x1b')
        alice.wait('sshfm  /tui')
        print('PASS: upstream mkdir/move/goto/delete confirmation, broadcast and bell', flush=True)
        command('rm', '-r', '/tui')
        alice.wait('sshfm  /  ')
        alice.send('q')
        alice.wait(b'\x1b[?1049l')
        assert alice.proc.wait(timeout=5) == 0
        print('PASS: TUI resize, tiny-terminal recovery, original confirmation, deleted-directory fallback and clean exit', flush=True)
    finally:
        alice.close()
        if bob:
            bob.close()
