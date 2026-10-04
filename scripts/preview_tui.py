#!/usr/bin/env python3
"""Local xterm.js preview of a real SSH PTY; run only during development."""

import argparse
import fcntl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import pty
import select
import signal
import struct
import subprocess
import tempfile
import termios
import threading
import tty
from urllib.request import urlopen


PAGE = r'''<!doctype html><html lang="zh"><meta charset="utf-8">
<title>sshfm TUI 本地预览</title><link rel="stylesheet" href="/xterm.css">
<style>body{background:#111827;color:#e5e7eb;font:15px system-ui;padding:20px}
button,select{padding:8px;margin:0 8px 14px 0}#terminal{display:inline-block;padding:14px;background:#10151c;border:1px solid #475569;border-radius:6px}</style>
<h2>sshfm · 真实 SSH 终端</h2>
<p>点击终端后使用键盘。Ctrl+S 保存，Ctrl+G 跳行，Esc 返回。</p>
<button id="focus">聚焦终端</button><button id="save">保存 Ctrl+S</button>
<button id="back">返回 Esc</button>
<label>终端尺寸 <select id="size"><option>90x24</option><option>40x12</option><option>120x30</option></select></label>
<div id="terminal"></div><p id="state">连接中</p><script src="/xterm.js"></script>
<script>
const term=new Terminal({cols:90,rows:24,fontSize:16,fontFamily:'Menlo, monospace',screenReaderMode:true,theme:{background:'#10151c'}});
term.open(document.querySelector('#terminal'));
let queue=Promise.resolve(), pending='';
function send(data){data=pending+data;pending='';const last=data.charCodeAt(data.length-1);if(last>=0xD800&&last<=0xDBFF){pending=data.slice(-1);data=data.slice(0,-1);}if(!data)return;queue=queue.catch(()=>{}).then(()=>fetch('/input',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({data})}));}
term.onData(send);document.querySelector('#focus').onclick=()=>term.focus();
document.querySelector('#save').onclick=()=>{send('\x13');term.focus()};
document.querySelector('#back').onclick=()=>{send('\x1b');term.focus()};
document.querySelector('#size').onchange=async e=>{const [width,height]=e.target.value.split('x').map(Number);term.resize(width,height);await fetch('/resize',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({width,height})});term.focus()};
async function poll(){try{const r=await fetch('/output');if(!r.ok)throw Error(r.status);const data=new Uint8Array(await r.arrayBuffer());if(data.length)term.write(data);document.querySelector('#state').textContent='已连接 · 本机 SSH PTY';}catch(e){document.querySelector('#state').textContent='连接结束: '+e;}setTimeout(poll,100)}poll();
</script></html>'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ssh-port', type=int, default=2223)
    parser.add_argument('--http-port', type=int, default=8893)
    args = parser.parse_args()
    # Public registry assets stay in memory; nothing is vendored into the repository.
    assets = {}
    for path, url in (
        ('/xterm.js', 'https://cdn.jsdelivr.net/npm/@xterm/xterm@5.5.0/lib/xterm.js'),
        ('/xterm.css', 'https://cdn.jsdelivr.net/npm/@xterm/xterm@5.5.0/css/xterm.css'),
    ):
        with urlopen(url, timeout=20) as response:
            assets[path] = response.read()
    master, slave = pty.openpty()
    tty.setraw(slave)
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 24, 90, 0, 0))
    lock = threading.Lock()
    with tempfile.TemporaryDirectory(prefix='sshfm-preview-') as temp:
        hosts = os.path.join(temp, 'known_hosts')
        scan = subprocess.run(['ssh-keyscan', '-T', '3', '-t', 'ed25519', '-p',
                               str(args.ssh_port), '127.0.0.1'], capture_output=True,
                              check=True, timeout=10)
        with open(hosts, 'wb') as stream:
            stream.write(scan.stdout)
        proc = subprocess.Popen(['ssh', '-F', '/dev/null', '-tt', '-p', str(args.ssh_port),
                                 '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
                                 '-o', 'UserKnownHostsFile=' + hosts, 'preview@127.0.0.1'],
                                stdin=slave, stdout=slave, stderr=slave,
                                env=dict(os.environ, TERM='xterm-256color'), start_new_session=True)
        os.close(slave)

        class Handler(BaseHTTPRequestHandler):
            def reply(self, data, content_type='text/plain', status=200):
                self.send_response(status)
                self.send_header('Content-Type', content_type)
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path == '/':
                    self.reply(PAGE.encode(), 'text/html; charset=utf-8')
                elif self.path in assets:
                    self.reply(assets[self.path], 'text/css' if self.path.endswith('.css') else 'text/javascript')
                elif self.path == '/output':
                    output = bytearray()
                    with lock:
                        while select.select([master], [], [], 0)[0]:
                            try:
                                output.extend(os.read(master, 65536))
                            except OSError:
                                break
                    self.reply(bytes(output), 'application/octet-stream')
                else:
                    self.reply(b'Not found', status=404)

            def do_POST(self):
                if self.headers.get('Origin') != f'http://127.0.0.1:{args.http_port}':
                    self.reply(b'Invalid origin', status=403)
                    return
                length = int(self.headers.get('Content-Length', '0'))
                if length > 1_000_000:
                    self.reply(b'Too large', status=413)
                    return
                data = json.loads(self.rfile.read(length))
                with lock:
                    if self.path == '/input':
                        os.write(master, data['data'].encode())
                    elif self.path == '/resize':
                        fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack(
                            'HHHH', data['height'], data['width'], 0, 0))
                        os.kill(proc.pid, signal.SIGWINCH)
                    else:
                        self.reply(b'Not found', status=404)
                        return
                self.reply(b'OK')

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', args.http_port), Handler)
        print(f'Preview: http://127.0.0.1:{args.http_port}', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)
            os.close(master)


if __name__ == '__main__':
    main()
