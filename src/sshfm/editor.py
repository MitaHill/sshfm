"""Grapheme-aware text editing and display cells, independent of SSH."""

from dataclasses import dataclass

import regex
from wcwidth import wcswidth


GRAPHEME = regex.compile(r'\X')


class CellWidth:
    """Widths calibrated per SSH client, without changing grapheme boundaries."""
    def __init__(self):
        self.zwj = True
        self.vs16 = True
        self.flags = True

    def __call__(self, text):
        width = 0
        for match in GRAPHEME.finditer(text):
            cluster = match.group()
            if not self.zwj:
                cluster = cluster.replace('\u200d', '')
            if not self.vs16:
                cluster = cluster.replace('\ufe0f', '')
            if not self.flags and len(cluster) == 2 and all(
                    0x1F1E6 <= ord(c) <= 0x1F1FF for c in cluster):
                width += sum(max(0, wcswidth(c)) for c in cluster)
            else:
                width += max(0, wcswidth(cluster))
        return width


def display(text):
    # User-controlled terminal escapes must never reach the renderer verbatim.
    return ''.join('^' + chr(ord(c) + 64) if ord(c) < 32
                   else '^?' if c == '\x7f' else f'<{ord(c):02X}>' if 128 <= ord(c) < 160
                   else c for c in text)


def display_ansi(text):
    # Same caret rendering as upstream disp_expand; only renderer-owned SGR escapes.
    return ''.join('\x1b[90m' + display(c) + '\x1b[39m' if ord(c) < 32 or ord(c) == 127
                   else display(c) for c in text)


def clip(text, width, cells=wcswidth):
    result, used = [], 0
    for match in GRAPHEME.finditer(display(text)):
        cluster = match.group()
        cluster_width = max(0, cells(cluster))
        if used + cluster_width > width:
            break
        result.append(cluster)
        used += cluster_width
    return ''.join(result)


def pad(text, width, cells=wcswidth):
    text = clip(text, width, cells)
    return text + ' ' * max(0, width - cells(text))


def previous(text, pos):
    return next((m.start() for m in reversed(list(GRAPHEME.finditer(text[:pos])))), 0)


def following(text, pos):
    match = GRAPHEME.match(text, pos)
    return match.end() if match else len(text)


@dataclass
class VisualLine:
    start: int
    end: int
    text: str
    number: int


class Editor:
    def __init__(self, text=''):
        self.text = text
        self.pos = 0
        self.goal = None

    def load(self, text):
        self.text = text
        self.pos = min(self.pos, len(text))
        # Re-clamp if the new contents put the old cursor inside a grapheme.
        self.pos = max((m.end() for m in GRAPHEME.finditer(text) if m.end() <= self.pos),
                       default=0)
        self.goal = None

    def replace(self, start, end, text):
        value = self.text[:start] + text + self.text[end:]
        if value == self.text:
            return False
        self.text = value
        self.pos = start + len(text)
        for match in GRAPHEME.finditer(value):
            if match.start() < self.pos < match.end():
                self.pos = match.end()
                break
        self.goal = None
        return True

    def edit(self, key, data=''):
        if key == 'backspace':
            return self.replace(previous(self.text, self.pos), self.pos, '')
        if key == 'delete':
            return self.replace(self.pos, following(self.text, self.pos), '')
        return self.replace(self.pos, self.pos, data)

    def rows(self, width, cells=wcswidth):
        rows, offset = [], 0
        width = max(1, width)
        for number, line in enumerate(self.text.split('\n'), 1):
            start, end, used, parts = offset, offset, 0, []
            for match in GRAPHEME.finditer(line):
                shown = display(match.group())
                cluster_width = max(0, cells(shown))
                if used + cluster_width > width and parts:
                    rows.append(VisualLine(start, end, ''.join(parts), number))
                    start, used, parts = end, 0, []
                parts.append(shown)
                used += cluster_width
                end = offset + match.end()
            rows.append(VisualLine(start, end, ''.join(parts), number))
            offset += len(line) + 1
        return rows

    def cursor(self, rows, cells=wcswidth):
        row = 0
        for index, line in enumerate(rows):
            if line.start <= self.pos <= line.end:
                row = index
        line = rows[row]
        column = max(0, cells(display(self.text[line.start:self.pos])))
        return row, column

    def move(self, key, width, height, cells=wcswidth):
        if key == 'left':
            self.pos = previous(self.text, self.pos)
        elif key == 'right':
            self.pos = following(self.text, self.pos)
        elif key == 'home':
            self.pos = self.text.rfind('\n', 0, self.pos) + 1
        elif key == 'end':
            end = self.text.find('\n', self.pos)
            self.pos = len(self.text) if end < 0 else end
        elif key in ('up', 'down', 'pageup', 'pagedown'):
            rows = self.rows(width, cells)
            row, col = self.cursor(rows, cells)
            if self.goal is None:
                self.goal = col
            delta = -1 if key in ('up', 'pageup') else 1
            delta *= height if key in ('pageup', 'pagedown') else 1
            if row + delta < 0:
                self.pos = 0
                return
            if row + delta >= len(rows):
                self.pos = len(self.text)
                return
            target = rows[row + delta]
            self.pos = target.start
            used = 0
            for match in GRAPHEME.finditer(self.text[target.start:target.end]):
                cluster_width = max(0, cells(display(match.group())))
                if used + cluster_width > self.goal:
                    break
                used += cluster_width
                self.pos = target.start + match.end()
            return
        self.goal = None

    def goto(self, number):
        lines = self.text.split('\n')
        if not 1 <= number <= len(lines):
            raise ValueError('line number is out of range')
        self.pos = sum(len(line) + 1 for line in lines[:number - 1])
        self.goal = None
