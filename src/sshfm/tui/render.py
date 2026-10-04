"""ANSI frame generation from state and a snapshot of session presence.

Rendering updates viewport/scroll state, but never accesses SSH or storage.
"""

from dataclasses import dataclass
from datetime import datetime, timezone, timedelta

from ..editor import GRAPHEME, clip, display, display_ansi, pad
from ..layout import HEADERS, marquee, size_text
from .state import layout as state_layout, metrics as state_metrics


@dataclass(frozen=True)
class Presence:
    locked: frozenset = frozenset()
    lock_ip: str = ''
    here: int = 0
    total: int = 0


def format_time(stamp, time_offset):
    if stamp <= 0:
        return '-'
    return (datetime.fromtimestamp(stamp / 1e9, timezone.utc) +
            timedelta(hours=time_offset)).strftime('%Y-%m-%d %H:%M')


class Renderer:
    def __init__(self, state, size, presence):
        self.state = state
        self.size = size
        self.presence = presence

    def editor_title(self):
        state = self.state
        line = state.editor.text.count('\n', 0, state.editor.pos) + 1
        total = state.editor.text.count('\n') + 1
        title = f' edit: {state.file["path"].lstrip("/")}   line {line}/{total}'
        if self.presence.lock_ip:
            title += f'   locked by {self.presence.lock_ip}'
        return title

    def header(self):
        state = self.state
        cols = state_layout(state, self.size).cols
        if state.file:
            return self.editor_title(), cols, 0
        here = self.presence.here
        title = f' sshfm  /{state.path.lstrip("/")}   [{state.ip}]   ol {here}/{self.presence.total}'
        if not state.banner:
            return title, cols, 0
        # Keep room for the banner even when a long path makes the title scroll.
        reserved = min(state.cells(display(state.banner)), max(1, cols // 3))
        width = min(state.cells(display(title)), cols - reserved - 2)
        return title, width, cols - width - 2

    def render(self):
        state = self.state
        layout = state_layout(state, self.size)
        cols, height = layout.cols, layout.rows
        editor_mode = state.file is not None
        cursor = None
        output = ['\x1b[?25l']
        if state.last_size != self.size:
            output.append('\x1b[2J')
            state.last_size = self.size

        def put(row, text='', reverse=False, width=None, raw=False):
            if width is None:
                width = cols
            shown = text if raw else pad(text, width, state.cells)
            output.append(f'\x1b[{row};1H\x1b[0m\x1b[K' + ('\x1b[7m' if reverse else '') +
                          shown + '\x1b[0m')

        title, title_width, banner_width = self.header()
        top = marquee(title, state.scroll, title_width, state.cells)
        if banner_width:
            banner = display(state.banner)
            if state.cells(banner) <= banner_width:
                top += '  ' + ' ' * (banner_width - state.cells(banner)) + banner
            else:
                top += '  ' + marquee(banner, state.banner_scroll, banner_width, state.cells)
        put(1, top, True)
        total, first, count = 0, 0, layout.height
        if editor_mode:
            text_width, _, numw = state_metrics(state, self.size)
            visual = state.editor.rows(text_width, state.cells)
            if state.last_text_width != text_width:
                state.editor_top = next((i for i, row in enumerate(visual) if row.number == state.anchor_line), 0)
                state.last_text_width = text_width
            crow, ccol = state.editor.cursor(visual, state.cells)
            if crow < state.editor_top:
                state.editor_top = crow
            elif crow >= state.editor_top + layout.height:
                state.editor_top = crow - layout.height + 1
            state.editor_top = max(0, min(state.editor_top, len(visual) - layout.height))
            for index in range(layout.height):
                vi = state.editor_top + index
                text = ''
                if vi < len(visual):
                    row = visual[vi]
                    number = (f'{row.number:>{numw - 1}} ' if vi == 0 or visual[vi - 1].number != row.number
                              else ' ' * numw)
                    text = number + display_ansi(state.editor.text[row.start:row.end]) + ' ' * max(0, layout.main_cols - numw - state.cells(row.text))
                put(layout.top + index, text if text else ' ' * layout.main_cols, raw=True)
            cursor = (crow - state.editor_top + layout.top, numw + ccol + 1)
            total, first = len(visual), state.editor_top
            state.anchor_line = visual[state.editor_top].number
        else:
            for index, group in enumerate(layout.groups):
                widths = layout.widths(group)
                text = ' '.join(pad(HEADERS[c], widths[c], state.cells) if c != 1 else
                                HEADERS[c].rjust(widths[c]) for c in group)
                put(2 + index, text, width=cols)
            state.top = max(0, min(state.top, max(0, len(state.entries) - layout.visible)))
            if state.selected < state.top:
                state.top = state.selected
            elif state.selected >= state.top + layout.visible:
                state.top = state.selected - layout.visible + 1
            row_number = layout.top
            for index in range(state.top, len(state.entries)):
                if row_number > layout.bottom:
                    break
                entry = state.entries[index]
                selected = index == state.selected
                values = [entry['name'] + ('/' if entry['kind'] == 'dir' else ''),
                          '<DIR>' if entry['kind'] == 'dir' else str(entry['size']),
                          format_time(entry['created_at'], state.time_offset), format_time(entry['updated_at'], state.time_offset),
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
                            value = marquee(value, state.name_scroll, widths[c], state.cells)
                        value = pad(value, widths[c], state.cells)
                        if c == 1:
                            value = value.strip().rjust(widths[c])
                        if c == 0 and entry['id'] in self.presence.locked:
                            value = ('\x1b[27m' if selected else '\x1b[7m') + value + (
                                     '\x1b[7m' if selected else '\x1b[27m')
                        parts.append(value)
                    put(row_number, ' '.join(parts), selected, raw=True)
                    row_number += 1
            for row in range(row_number, layout.bottom + 1):
                put(row, '', width=layout.main_cols)
            total, first, count = len(state.entries), state.top, layout.visible
        # The rightmost cell is dedicated to the upstream scrollbar.
        thumb = max(1, layout.height * count // max(1, total))
        start = first * (layout.height - thumb) // max(1, total - count)
        for row in range(layout.height):
            bar = ' ' if total <= count else '║' if start <= row < start + thumb else '│'
            output.append(f'\x1b[{layout.top + row};{cols}H\x1b[0m{bar}')
        for index, keys in enumerate(layout.keys):
            put(height - 1 - len(layout.keys) + index, keys, True)
        if state.status != state.msg_shown:
            state.msg_scroll = 0
            state.msg_shown = state.status
        put(height - 1, marquee(state.status, state.msg_scroll, cols, state.cells))
        put(height)
        if state.mode == 'prompt':
            label = state.prompt_label
            labw = state.cells(label)
            available = max(1, cols - labw)
            textw = max(1, available - 1)
            x = state.cells(display(state.prompt.text))
            minimum = max(1, min(x, available // 4))
            showw = max(1, minimum, min(x, textw))
            curw = state.cells(display(state.prompt.text[:state.prompt.pos]))
            if curw < state.prompt_scroll:
                state.prompt_scroll = curw
            if curw - state.prompt_scroll > showw:
                state.prompt_scroll = curw - showw
            if x > minimum and curw - state.prompt_scroll < minimum:
                state.prompt_scroll = curw - minimum
            state.prompt_scroll = max(0, state.prompt_scroll)
            start, used = 0, 0
            for match in GRAPHEME.finditer(state.prompt.text):
                if used >= state.prompt_scroll:
                    break
                used += state.cells(display(match.group()))
                start = match.end()
            state.prompt_scroll = used
            put(height, label + clip(state.prompt.text[start:], showw, state.cells))
            cursor = (height, labw + max(0, curw - used) + 1)
        if cursor and (editor_mode or state.mode == 'prompt'):
            row, column = cursor
            output.append(f'\x1b[{min(height, row)};{min(cols, column)}H\x1b[?25h')
        return ''.join(output)

    def marquee_active(self):
        state = self.state
        layout = state_layout(state, self.size)
        if state.cells(display(state.status)) > layout.cols:
            return True
        title, title_width, banner_width = self.header()
        if banner_width and state.cells(display(state.banner)) > banner_width:
            return True
        if not state.file:
            entry = state.selected_entry()
            if entry and state.cells(display(entry['name'] + ('/' if entry['kind'] == 'dir' else ''))) > layout.widths(layout.groups[0])[0]:
                return True
        return state.cells(display(title)) > title_width
