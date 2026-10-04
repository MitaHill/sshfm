"""Full-screen file browser and editor on a raw SSH terminal channel."""

import asyncio
from collections import deque
from datetime import datetime, timezone, timedelta
import time
import logging
import re
import sqlite3
import uuid

import asyncssh
from prompt_toolkit.input.vt100_parser import Vt100Parser
from prompt_toolkit.key_binding.key_processor import KeyPress
from prompt_toolkit.keys import Keys

from .editor import CellWidth, Editor, GRAPHEME, clip, display, display_ansi, pad
from .store import StoreError
from .collation import name_key
from .layout import HEADERS, Layout, marquee, size_text


class TUI:
    def __init__(self, process, service, time_offset=0):
        self.process = process
        self.service = service
        self.owner = uuid.uuid4().hex
        self.ip = process.get_extra_info('peername')[0]
        self.time_offset = time_offset
        self.cwd_id = 1
        self.path = '/'
        self.entries = []
        self.selected = 0
        self.top = 0
        self.sort = 'time'
        self.file = None
        self.editor = Editor()
        self.saved_text = ''
        self.editor_top = 0
        self.mode = 'browser'
        self.return_mode = 'browser'
        self.prompt_action = None
        self.prompt_label = ''
        self.prompt = Editor()
        self.prompt_target = None
        self.status = ''
        self.scroll = 0
        self.name_scroll = 0
        self.msg_scroll = 0
        self.msg_shown = ''
        self.prompt_scroll = 0
        self.anchor_line = 1
        self.last_text_width = None
        self.redraw = True
        self.running = True
        self.version = -1
        self.last_size = None
        self.keys = deque()
        self.parser = Vt100Parser(self.keys.append)
        self.cells = CellWidth()
        self.last_click = None

    @property
    def dirty(self):
        return self.file is not None and self.editor.text != self.saved_text

    def size(self):
        width, height, _, _ = self.process.get_terminal_size()
        return width or 80, height or 24

    def layout(self):
        return Layout(*self.size(), self.file is not None, self.mode == 'prompt', self.cells)

    def metrics(self):
        layout = self.layout()
        number_width = max(3, len(str(self.editor.text.count('\n') + 1))) + 1
        return max(1, layout.main_cols - number_width - 1), layout.height, number_width

    def sort_entries(self):
        self.entries.sort(key=lambda item: ((-(item['updated_at'] // 1_000_000_000),)
                                            if self.sort == 'time' else ()) +
                          (name_key(item['name']),))

    def absolute(self, path):
        if not path.startswith('/'):
            path = self.path.rstrip('/') + '/' + path
        return '/' + '/'.join(self.service.store.parts(path))

    def selected_entry(self):
        return self.entries[self.selected] if self.entries else None

    async def refresh(self, force=False):
        if not force and self.version == self.service.version:
            return
        selected = self.selected_entry()
        selected_id = selected['id'] if selected else None
        version = self.service.version
        view = await self.service.read(self.service.store.view, self.cwd_id,
                                       self.file['id'] if self.file else None)
        if self.cwd_id != view['cwd_id'] or self.path != view['path']:
            self.entries = []
            self.selected = self.top = 0
            selected_id = None
        self.cwd_id, self.path = view['cwd_id'], view['path']
        listed = {item['id']: item for item in view['entries']}
        kept = [listed[item['id']] for item in self.entries if item['id'] in listed]
        if kept:
            old_ids = {item['id'] for item in self.entries}
            top_id = self.entries[self.top]['id'] if self.top < len(self.entries) else None
            self.top = next((i for i, item in enumerate(kept) if item['id'] == top_id), 0)
            added = [item for item in view['entries'] if item['id'] not in old_ids]
            self.entries = kept[:self.top] + added + kept[self.top:]
        else:
            self.entries = view['entries']
            self.sort = 'time'
            self.sort_entries()
            self.top = 0
        self.selected = next((i for i, item in enumerate(self.entries)
                              if item['id'] == selected_id),
                             min(self.selected, max(0, len(self.entries) - 1)))
        if self.file:
            current = view['file']
            if current is None:
                self.file = None
                self.mode = 'browser'
                self.prompt_action = None
                self.status = 'File was deleted; returned to browser.'
            else:
                if not self.dirty and current['revision'] != self.file['revision']:
                    self.editor.load(current['content'])
                    self.saved_text = current['content']
                    self.status = ''
                self.file = current
        self.version = version
        self.redraw = True

    def ask(self, action, label, value='', target=None):
        self.return_mode = self.mode
        self.mode = 'prompt'
        self.prompt_action = action
        self.prompt_label = label
        self.prompt = Editor(value)
        self.prompt.pos = len(value)
        self.prompt_target = target
        self.prompt_scroll = 0

    def cancel_prompt(self):
        self.mode = self.return_mode
        self.prompt_action = None

    async def confirm_prompt(self):
        action, value = self.prompt_action, self.prompt.text
        self.cancel_prompt()
        if action == 'discard':
            if value == 'esc':
                self.close_editor()
            else:
                self.status = 'discard cancelled'
            return
        if action == 'delete':
            if value != 'del':
                self.status = 'not deleted'
                return
            path, entry_id = self.prompt_target
            result = await self.service.remove(path, recursive=True, expected_id=entry_id)
            self.status = 'deleted (locked entries kept)' if result.get('partial') else 'deleted'
        elif not value:
            return
        elif action in ('file', 'dir'):
            if value in ('.', '..') or '/' in value:
                self.status = 'invalid name'
                return
            created = await self.service.create(self.absolute(value), action,
                                                '' if action == 'file' else None, self.ip)
            self.status = ('created ' if action == 'file' else 'mkdir ') + value
            await self.refresh(force=True)
            self.selected = next(i for i, item in enumerate(self.entries) if item['id'] == created['id'])
        elif action == 'move':
            path, entry_id = self.prompt_target
            destination = self.absolute(value)
            if destination == path:
                self.status = 'same path'
                return
            await self.service.move(path, destination, entry_id, self.ip)
            self.status = 'moved'
        elif action == 'goto':
            path = self.absolute(value)
            if path == '/' and not value.endswith('/'):
                self.status = "can't select the root. please goto the /"
                return
            try:
                target = await self.service.read(self.service.store.stat, path)
            except StoreError:
                self.status = 'not found: ' + value
                return
            if value.endswith('/'):
                if target['kind'] != 'dir':
                    self.status = 'exists but is not a directory'
                    return
                if self.cwd_id == target['id']:
                    self.status = 'same directory and no action'
                    return
                self.cwd_id = target['id']
                self.selected = self.top = 0
            else:
                self.cwd_id = target['parent_id']
            await self.refresh(force=True)
            if not value.endswith('/'):
                self.selected = next(i for i, item in enumerate(self.entries) if item['id'] == target['id'])
            self.status = 'goto ' + (path.lstrip('/') + '/' if value.endswith('/') and path != '/'
                                     else path.lstrip('/') if path != '/' else '/')
        elif action == 'line':
            match = re.match(r'\s*([+-]?\d+)', value)
            number = int(match[1]) if match else 0
            self.editor.goto(max(1, min(self.editor.text.count('\n') + 1, number)))
        elif action == 'broadcast':
            self.service.broadcast(self.owner, value)
        await self.refresh(force=True)

    async def open_file(self, entry_id):
        self.file = await self.service.read(self.service.store.read_id, entry_id)
        self.editor = Editor(self.file['content'])
        self.saved_text = self.file['content']
        self.editor_top = 0
        self.mode = 'editor'
        self.status = ''

    def close_editor(self):
        if self.file:
            self.service.release(self.file['id'], self.owner)
        self.file = None
        self.mode = 'browser'
        self.prompt_action = None
        self.status = ''
        self.version = -1

    async def edit(self, key, data=''):
        if key == 'backspace' and self.editor.pos == 0:
            return
        if key == 'delete' and self.editor.pos == len(self.editor.text):
            return
        if self.service.locks.get(self.file['id']) != self.owner:
            latest = await self.service.begin_edit(self.file['id'], self.owner)
            if latest['revision'] != self.file['revision']:
                self.editor.load(latest['content'])
                self.saved_text = latest['content']
            self.file = latest
        self.editor.edit(key, data)
        if not self.dirty:
            self.service.release(self.file['id'], self.owner)
        self.status = ''

    async def save(self):
        if not self.dirty:
            self.status = 'saved'
            return
        try:
            result = await self.service.save(self.file['id'], self.file['revision'],
                                             self.editor.text, self.owner, self.ip)
        except (StoreError, sqlite3.Error):
            self.status = 'save failed'
            raise
        self.file.update(result)
        self.saved_text = self.editor.text
        self.status = 'saved'

    async def handle_key(self, event):
        key = event.key.value.strip('<>') if hasattr(event.key, 'value') else event.key
        data = event.data
        key = {'c-m': 'enter', 'c-j': 'enter', 'c-h': 'backspace',
               'c-i': 'tab'}.get(key, key)
        if key == 'vt100-mouse-event':
            await self.handle_mouse(data)
            return
        if key == 'cpr-response':
            response = re.fullmatch(r'\x1b\[(\d+);(\d+)R', data)
            if response:
                row, column = map(int, response.groups())
                if row in (1, 2, 3) and 1 <= column <= 20:
                    attribute = {1: 'vs16', 2: 'zwj', 3: 'flags'}[row]
                    setattr(self.cells, attribute, column == 3)
            return
        self.last_click = None
        if self.mode == 'prompt':
            if key == 'escape':
                self.cancel_prompt()
            elif key == 'enter':
                await self.confirm_prompt()
            elif key in ('left', 'right'):
                self.prompt.move(key, 80, 1)
            elif key == 'backspace':
                self.prompt.edit(key)
            elif key == 'bracketed-paste':
                self.prompt.edit('insert', data.replace('\r', '').replace('\n', ' '))
            elif len(data) == 1 and ord(data) >= 32 and key not in ('cpr-response',):
                self.prompt.edit('insert', data)
            return
        if self.mode == 'editor':
            width, height, _ = self.metrics()
            if key in ('left', 'right', 'up', 'down', 'home', 'end', 'pageup', 'pagedown'):
                if key in ('pageup', 'pagedown'):
                    visual = self.editor.rows(width, self.cells)
                    row, col = self.editor.cursor(visual, self.cells)
                    screen_row = max(0, min(height - 1, row - self.editor_top))
                    self.editor_top = max(0, min(max(0, len(visual) - height), self.editor_top +
                                                 (-height if key == 'pageup' else height)))
                    target = visual[min(len(visual) - 1, self.editor_top + screen_row)]
                    goal = self.editor.goal if self.editor.goal is not None else col
                    self.editor.pos, used = target.start, 0
                    for match in GRAPHEME.finditer(self.editor.text[target.start:target.end]):
                        amount = self.cells(display(match.group()))
                        if used + amount > goal:
                            break
                        used += amount
                        self.editor.pos = target.start + match.end()
                    self.editor.goal = goal
                else:
                    self.editor.move(key, width, height, self.cells)
            elif key == 'c-s':
                await self.save()
            elif key in ('escape', 'c-c'):
                if self.dirty:
                    self.ask('discard', "type 'esc' to discard changes: ")
                else:
                    self.close_editor()
            elif key == 'c-g':
                self.ask('line', 'goto line: ')
            elif key in ('backspace', 'delete'):
                await self.edit(key)
            elif key == 'enter':
                await self.edit('insert', '\n')
            elif key == 'bracketed-paste':
                await self.edit('insert', data.replace('\r\n', '\n').replace('\r', '\n'))
            elif len(data) == 1 and (ord(data) >= 32 or key not in
                    ('c-b', 'c-c', 'c-s', 'c-g', 'c-l', 'c-d', 'tab', 'escape')):
                await self.edit('insert', data)
            return
        self.name_scroll = 0
        entry = self.selected_entry()
        layout = self.layout()
        height = layout.visible
        if key in ('up', 'down', 'pageup', 'pagedown', 'home', 'end', 'left', 'right'):
            if key in ('home', 'left'):
                self.selected = 0
            elif key in ('end', 'right'):
                self.selected = len(self.entries) - 1
            else:
                if key in ('pageup', 'pagedown'):
                    relative = self.selected - self.top
                    self.top = max(0, min(max(0, len(self.entries) - height),
                                         self.top + (-height if key == 'pageup' else height)))
                    self.selected = self.top + relative
                else:
                    self.selected += -1 if key == 'up' else 1
            self.selected = max(0, min(self.selected, len(self.entries) - 1))
        elif key == 'enter' and entry:
            if entry['kind'] == 'dir':
                self.cwd_id = entry['id']
                self.selected = self.top = 0
                await self.refresh(force=True)
            else:
                await self.open_file(entry['id'])
        elif key == 'escape' and self.cwd_id != 1:
            parent = await self.service.read(self.service.store.stat, self.path)
            self.cwd_id = parent['parent_id']
            self.selected = self.top = 0
            await self.refresh(force=True)
        elif data in ('n', 'N'):
            self.ask('file' if data == 'n' else 'dir', 'new file: ' if data == 'n' else 'new dir: ')
        elif data in ('m', 'd') and entry:
            path = self.path.rstrip('/') + '/' + entry['name']
            if data == 'm':
                self.ask('move', 'move to: ', target=(path, entry['id']))
            else:
                self.status = 'delete ' + path.lstrip('/') + ' ?'
                self.ask('delete', "type 'del' to confirm: ",
                         target=(path, entry['id']))
        elif data == 'g':
            self.ask('goto', 'goto: ')
        elif data in ('s', 't', 'r'):
            if data != 'r':
                self.sort = 'name' if data == 's' else 'time'
                selected_id = entry['id'] if entry else None
                relative = self.selected - self.top
                self.sort_entries()
                self.selected = next((i for i, e in enumerate(self.entries) if e['id'] == selected_id), 0)
                self.top = max(0, self.selected - relative)
            await self.refresh(force=True)
        elif data == 'B':
            self.ask('broadcast', 'broadcast: ')
        elif data == 'b' or key == 'c-b':
            self.service.bell(None if key == 'c-b' else self.cwd_id)
        elif data == 'q':
            self.running = False

    def place_cursor(self, editor, start, end, column):
        editor.pos, used = start, 0
        for match in GRAPHEME.finditer(editor.text[start:end]):
            amount = self.cells(display(match.group()))
            if used + amount > column:
                break
            used += amount
            editor.pos = start + match.end()
        editor.goal = None

    async def handle_mouse(self, data):
        match = re.fullmatch(r'\x1b\[<(\d+);(\d+);(\d+)([Mm])', data)
        if match:
            button, column, row = map(int, match.groups()[:3])
            released = match[4] == 'm'
        elif data.startswith('\x1b[M') and len(data) == 6:
            button, column, row = (ord(c) - 32 for c in data[3:])
            released = button & 3 == 3
        else:
            return
        cols, rows = self.size()
        if released or button & 32 or not (1 <= column <= cols and 1 <= row <= rows):
            return
        layout = self.layout()
        # Modifiers use bits 2–4; ignore them when identifying the button.
        button &= ~28
        if self.mode == 'prompt':
            if button == 0 and row == rows:
                self.place_cursor(self.prompt, 0, len(self.prompt.text),
                                  max(0, column - 1 - self.cells(self.prompt_label) +
                                      self.prompt_scroll))
            return
        if not layout.top <= row <= layout.bottom:
            return
        if button in (64, 65):
            self.last_click = None
            delta = -3 if button == 64 else 3
            if self.mode == 'editor':
                width, height, _ = self.metrics()
                visual = self.editor.rows(width, self.cells)
                crow, ccol = self.editor.cursor(visual, self.cells)
                self.editor_top = max(0, min(max(0, len(visual) - height),
                                             self.editor_top + delta))
                target = max(self.editor_top, min(crow, self.editor_top + height - 1))
                if target != crow:
                    line = visual[target]
                    self.place_cursor(self.editor, line.start, line.end, ccol)
            else:
                self.top = max(0, min(max(0, len(self.entries) - layout.visible),
                                     self.top + delta))
                self.selected = max(self.top, min(self.selected,
                                                  self.top + layout.visible - 1))
            return
        if button != 0 or column == layout.cols:
            return
        if self.mode == 'editor':
            width, _, numw = self.metrics()
            visual = self.editor.rows(width, self.cells)
            index = self.editor_top + row - layout.top
            if index < len(visual):
                line = visual[index]
                self.place_cursor(self.editor, line.start, line.end,
                                  max(0, column - numw - 1))
        else:
            index = self.top + (row - layout.top) // len(layout.groups)
            if index >= len(self.entries):
                self.last_click = None
                return
            entry = self.entries[index]
            now = time.monotonic()
            click = (self.cwd_id, entry['id'], column, row)
            double = (self.last_click is not None and self.last_click[0] == click and
                      now - self.last_click[1] <= .4)
            self.selected = index
            self.name_scroll = 0
            self.last_click = None if double else (click, now)
            if double:
                await self.handle_key(KeyPress(Keys.ControlM, '\r'))

    def editor_title(self):
        line = self.editor.text.count('\n', 0, self.editor.pos) + 1
        total = self.editor.text.count('\n') + 1
        title = f' edit: {self.file["path"].lstrip("/")}   line {line}/{total}'
        owner = self.service.locks.get(self.file['id'])
        session = self.service.sessions.get(owner)
        if session is not None:
            title += f'   locked by {session.ip}'
        return title

    def render(self):
        layout = self.layout()
        cols, height = layout.cols, layout.rows
        editor_mode = self.file is not None
        cursor = None
        output = ['\x1b[?25l']
        if self.last_size != self.size():
            output.append('\x1b[2J')
            self.last_size = self.size()

        def put(row, text='', reverse=False, width=None, raw=False):
            if width is None:
                width = cols
            shown = text if raw else pad(text, width, self.cells)
            output.append(f'\x1b[{row};1H\x1b[0m\x1b[K' + ('\x1b[7m' if reverse else '') +
                          shown + '\x1b[0m')

        here = sum(ui.cwd_id == self.cwd_id for ui in self.service.sessions.values())
        title = (self.editor_title()
                 if editor_mode else f' sshfm  /{self.path.lstrip("/")}   [{self.ip}]   ol {here}/{len(self.service.sessions)}')
        put(1, marquee(title, self.scroll, cols, self.cells), True)
        total, first, count = 0, 0, layout.height
        if editor_mode:
            text_width, _, numw = self.metrics()
            visual = self.editor.rows(text_width, self.cells)
            if self.last_text_width != text_width:
                self.editor_top = next((i for i, row in enumerate(visual) if row.number == self.anchor_line), 0)
                self.last_text_width = text_width
            crow, ccol = self.editor.cursor(visual, self.cells)
            if crow < self.editor_top:
                self.editor_top = crow
            elif crow >= self.editor_top + layout.height:
                self.editor_top = crow - layout.height + 1
            self.editor_top = max(0, min(self.editor_top, len(visual) - layout.height))
            for index in range(layout.height):
                vi = self.editor_top + index
                text = ''
                if vi < len(visual):
                    row = visual[vi]
                    number = (f'{row.number:>{numw - 1}} ' if vi == 0 or visual[vi - 1].number != row.number
                              else ' ' * numw)
                    text = number + display_ansi(self.editor.text[row.start:row.end]) + ' ' * max(0, layout.main_cols - numw - self.cells(row.text))
                put(layout.top + index, text if text else ' ' * layout.main_cols, raw=True)
            cursor = (crow - self.editor_top + layout.top, numw + ccol + 1)
            total, first = len(visual), self.editor_top
            self.anchor_line = visual[self.editor_top].number
        else:
            for index, group in enumerate(layout.groups):
                widths = layout.widths(group)
                text = ' '.join(pad(HEADERS[c], widths[c], self.cells) if c != 1 else
                                HEADERS[c].rjust(widths[c]) for c in group)
                put(2 + index, text, width=cols)
            self.top = max(0, min(self.top, max(0, len(self.entries) - layout.visible)))
            if self.selected < self.top:
                self.top = self.selected
            elif self.selected >= self.top + layout.visible:
                self.top = self.selected - layout.visible + 1
            row_number = layout.top
            for index in range(self.top, len(self.entries)):
                if row_number > layout.bottom:
                    break
                entry = self.entries[index]
                selected = index == self.selected
                values = [entry['name'] + ('/' if entry['kind'] == 'dir' else ''),
                          '<DIR>' if entry['kind'] == 'dir' else str(entry['size']),
                          self.format_time(entry['created_at']), self.format_time(entry['updated_at']),
                          entry['creator_ip'], entry['editor_ip']]
                for group in layout.groups:
                    if row_number > layout.bottom:
                        break
                    widths = layout.widths(group)
                    parts = []
                    for c in group:
                        value = values[c]
                        if c == 1 and entry['kind'] != 'dir':
                            value = size_text(entry['size'], widths[c])
                        if c == 0 and selected:
                            value = marquee(value, self.name_scroll, widths[c], self.cells)
                        value = pad(value, widths[c], self.cells)
                        if c == 1:
                            value = value.strip().rjust(widths[c])
                        if c == 0 and entry['id'] in self.service.locks:
                            value = ('\x1b[27m' if selected else '\x1b[7m') + value + (
                                     '\x1b[7m' if selected else '\x1b[27m')
                        parts.append(value)
                    put(row_number, ' '.join(parts), selected, raw=True)
                    row_number += 1
            for row in range(row_number, layout.bottom + 1):
                put(row, '', width=layout.main_cols)
            total, first, count = len(self.entries), self.top, layout.visible
        # The rightmost cell is dedicated to the upstream scrollbar.
        thumb = max(1, layout.height * count // max(1, total))
        start = first * (layout.height - thumb) // max(1, total - count)
        for row in range(layout.height):
            bar = ' ' if total <= count else '║' if start <= row < start + thumb else '│'
            output.append(f'\x1b[{layout.top + row};{cols}H\x1b[0m{bar}')
        for index, keys in enumerate(layout.keys):
            put(height - 1 - len(layout.keys) + index, keys, True)
        if self.status != self.msg_shown:
            self.msg_scroll = 0
            self.msg_shown = self.status
        put(height - 1, marquee(self.status, self.msg_scroll, cols, self.cells))
        put(height)
        if self.mode == 'prompt':
            label = self.prompt_label
            labw = self.cells(label)
            available = max(1, cols - labw)
            textw = max(1, available - 1)
            x = self.cells(display(self.prompt.text))
            minimum = max(1, min(x, available // 4))
            showw = max(1, minimum, min(x, textw))
            curw = self.cells(display(self.prompt.text[:self.prompt.pos]))
            if curw < self.prompt_scroll:
                self.prompt_scroll = curw
            if curw - self.prompt_scroll > showw:
                self.prompt_scroll = curw - showw
            if x > minimum and curw - self.prompt_scroll < minimum:
                self.prompt_scroll = curw - minimum
            self.prompt_scroll = max(0, self.prompt_scroll)
            start, used = 0, 0
            for match in GRAPHEME.finditer(self.prompt.text):
                if used >= self.prompt_scroll:
                    break
                used += self.cells(display(match.group()))
                start = match.end()
            self.prompt_scroll = used
            put(height, label + clip(self.prompt.text[start:], showw, self.cells))
            cursor = (height, labw + max(0, curw - used) + 1)
        if cursor and (editor_mode or self.mode == 'prompt'):
            row, column = cursor
            output.append(f'\x1b[{min(height, row)};{min(cols, column)}H\x1b[?25h')
        self.process.stdout.write(''.join(output))
        self.redraw = False

    def marquee_active(self):
        layout = self.layout()
        if self.cells(display(self.status)) > layout.cols:
            return True
        if self.file:
            title = self.editor_title()
        else:
            title = f' sshfm  /{self.path.lstrip("/")}   [{self.ip}]   ol 0/{len(self.service.sessions)}'
            entry = self.selected_entry()
            if entry and self.cells(display(entry['name'] + ('/' if entry['kind'] == 'dir' else ''))) > layout.widths(layout.groups[0])[0]:
                return True
        return self.cells(display(title)) > layout.cols

    def format_time(self, stamp):
        if stamp <= 0:
            return '-'
        return (datetime.fromtimestamp(stamp / 1e9, timezone.utc) +
                timedelta(hours=self.time_offset)).strftime('%Y-%m-%d %H:%M')

    async def startup_flash(self):
        if getattr(self.process.stdout, 'limited', False):
            return
        for frame in range(8):
            cols, rows = self.size()
            reverse = frame % 2 == 0
            output = ['\x1b[?25l\x1b[2J\x1b[H', '\x1b[7m' if reverse else '\x1b[27m']
            output.extend(f'\x1b[{row};1H' + ' ' * cols for row in range(1, rows + 1))
            output.append(('\x1b[27m' if reverse else '\x1b[7m') +
                          f'\x1b[{max(1, rows // 2)};{max(1, (cols - 9) // 2)}H  sshfm  \x1b[0m')
            self.process.stdout.write(''.join(output))
            await asyncio.sleep(.09)
        self.process.stdout.write('\x1b[0m\x1b[2J\x1b[H\x1b[?25l')

    async def run(self):
        self.service.register(self.owner, self)
        self.process.stdout.write('\x1b[?1049h\x1b[?2004h\x1b[?1000h\x1b[?1006h\x1b[2J')
        reader = None
        try:
            await self.startup_flash()
            # Replies include their row, so late CPR responses remain unambiguous.
            for row, sample in enumerate(('❤️', '👩‍💻', '🇹🇼'), 1):
                self.process.stdout.write(f'\x1b[{row};1H{sample}\x1b[6n')
            await self.refresh(force=True)
            reader = asyncio.create_task(self.process.stdin.read(4096))
            last_tick = time.monotonic()
            while self.running:
                await self.refresh()
                if time.monotonic() - last_tick >= .1 and self.marquee_active():
                    self.scroll += 1
                    self.name_scroll += 1
                    self.msg_scroll += 1
                    self.redraw = True
                    last_tick = time.monotonic()
                if (self.redraw or self.last_size != self.size()) and getattr(self.process.stdout, 'ready', True):
                    self.render()
                ready, _ = await asyncio.wait([reader], timeout=0.1)
                if not ready:
                    self.parser.flush()
                else:
                    try:
                        data = reader.result()
                    except asyncssh.TerminalSizeChanged:
                        data = None
                        self.redraw = True
                    except (asyncssh.SignalReceived, asyncssh.BreakReceived):
                        data = None
                        self.keys.append(KeyPress(Keys.ControlC, '\x03'))
                    if data == '':
                        break
                    if data:
                        self.parser.feed(data)
                    reader = asyncio.create_task(self.process.stdin.read(4096))
                while self.keys and self.running:
                    key = self.keys.popleft()
                    action = self.prompt_action if self.mode == 'prompt' else None
                    try:
                        await self.handle_key(key)
                    except (StoreError, ValueError) as exc:
                        if self.status != 'save failed':
                            if 'being edited' in str(exc):
                                self.status = 'file is being edited by someone else'
                            else:
                                self.status = {'file': 'create failed', 'dir': 'mkdir failed',
                                               'move': 'move failed', 'delete': 'delete failed',
                                               'goto': 'path is outside the root'}.get(action, str(exc))
                    except sqlite3.Error:
                        logging.exception('TUI database operation failed')
                        self.status = 'save failed' if self.file else 'database operation failed'
                    self.redraw = True
        finally:
            self.service.disconnect(self.owner)
            if reader:
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)
            if not self.process.stdout.is_closing():
                self.process.stdout.write('\x1b[0m\x1b[?1000l\x1b[?1006l\x1b[?2004l\x1b[?25h\x1b[?1049l'
                                          'Disconnect from sshfm.\r\nSee you.\r\n')
