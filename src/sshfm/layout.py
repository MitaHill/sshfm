"""Port of the upstream six-column layout and bottom bars.

Reference: MitaHill/sshfm fd263ad0a85d36cce78921973f1612b353e12831,
UI::layout, entry_lines, cell_widths, cyclic_window and fmt_size.
"""
from .editor import GRAPHEME, clip, display, pad

CELL_MIN = (18, 8, 16, 16, 15, 15)
HEADERS = ('NAME', 'SIZE', 'CREATED', 'EDITED', 'CREATOR', 'EDITOR')
BROWSER_HINTS = ('n:new', 'N:mkdir', 'm:move', 'd:delete', 'Enter:open', 'g:goto',
                 'b:bel', 'B:bcast', '^B:bel-all', 't:time', 's:name', 'r:reload',
                 'Esc:back', 'q:quit')


class Layout:
    def __init__(self, cols, rows, editor, prompt, cells):
        self.cols, self.rows = max(12, cols), max(6, rows)
        self.main_cols = self.cols - 1
        self.groups = []
        current, used = [], 0
        for index, width in enumerate(CELL_MIN):
            extra = width + bool(current)
            if current and used + extra > self.main_cols:
                self.groups.append(current)
                current, used = [], 0
            used += width + bool(current)
            current.append(index)
        self.groups.append(current)
        hints = ('Enter:ok', 'Esc:cancel') if prompt else (
            ('^S:save', '^G:goto', 'Esc:back') if editor else BROWSER_HINTS)
        lines, current, used = [], [], 0
        for hint in hints:
            if current and used + cells(hint) + len(current) + 1 > self.cols:
                lines.append(current)
                current, used = [], 0
            current.append(hint)
            used += cells(hint)
        lines.append(current)
        self.keys = []
        for line in lines:
            base, remainder = divmod(max(len(line), self.cols - sum(map(cells, line))), len(line))
            self.keys.append(''.join(h + ' ' * (base + (i < remainder))
                                     for i, h in enumerate(line)))
        self.top = 2 if editor else 2 + len(self.groups)
        self.bottom = max(self.top, self.rows - 2 - len(self.keys))
        self.height = self.bottom - self.top + 1
        self.visible = max(1, self.height // len(self.groups))

    def widths(self, group):
        extra = max(0, self.main_cols - sum(CELL_MIN[c] for c in group) - len(group) + 1)
        base, remainder = divmod(extra, len(group))
        return {c: CELL_MIN[c] + base + (i < remainder) for i, c in enumerate(group)}


def marquee(text, offset, width, cells):
    if cells(display(text)) <= width:
        return pad(text, width, cells)
    clusters = GRAPHEME.findall(display(text) + '   ')
    # Upstream offsets are display cells. Start on the next whole cluster.
    period = sum(map(cells, clusters))
    offset %= max(1, period)
    start, used = 0, 0
    while used < offset:
        used += cells(clusters[start])
        start += 1
    text = ''.join(clusters[start:] + clusters + clusters[:start])
    return pad(clip(text, width, cells), width, cells)


def size_text(size, width):
    if size < 1024:
        return f'{size} B'
    amount = size
    units = ('B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB', 'EiB')
    unit = 0
    while amount >= 1024 and unit < len(units) - 1:
        amount /= 1024
        unit += 1
    if round(amount, 1) >= 1024 and unit < len(units) - 1:
        amount /= 1024
        unit += 1
    text = f'{amount:.1f} {units[unit]}'
    return text if len(text) <= width else f'{amount:.0f} {units[unit]}'
