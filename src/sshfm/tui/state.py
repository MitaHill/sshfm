"""Interface state and local transitions, independent of SSH and storage."""

from ..collation import name_key
from ..editor import CellWidth, Editor
from ..layout import Layout


def layout(state, size):
    return Layout(*size, state.file is not None, state.mode == 'prompt', state.cells)


def metrics(state, size):
    view = layout(state, size)
    number_width = max(3, len(str(state.editor.text.count('\n') + 1))) + 1
    return max(1, view.main_cols - number_width - 1), view.height, number_width


class State:
    def __init__(self, ip='', time_offset=0, banner=''):
        self.ip = ip
        self.time_offset = time_offset
        self.banner = banner
        self.banner_scroll = 0
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
        self.cells = CellWidth()
        self.last_click = None

    @property
    def dirty(self):
        return self.file is not None and self.editor.text != self.saved_text

    def sort_entries(self):
        self.entries.sort(key=lambda item: ((-(item['updated_at'] // 1_000_000_000),)
                                            if self.sort == 'time' else ()) +
                          (name_key(item['name']),))

    def selected_entry(self):
        return self.entries[self.selected] if self.entries else None

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
