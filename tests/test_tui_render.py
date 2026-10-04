import unittest

from sshfm.editor import Editor
from sshfm.tui.render import Presence, Renderer
from sshfm.tui.state import State


class RenderTests(unittest.TestCase):
    def test_browser_renders_without_ssh_or_storage(self):
        state = State('192.0.2.1', banner='公告 👩‍💻')
        state.entries = [dict(id=2, name='笔记', kind='file', size=3,
                              created_at=0, updated_at=0,
                              creator_ip='', editor_ip='')]
        renderer = Renderer(state, (90, 24), Presence(frozenset({2}), here=1, total=2))
        frame = renderer.render()
        self.assertIn('ol 1/2', frame)
        self.assertIn('公告 👩‍💻', frame)
        self.assertIn('笔记', frame)
        self.assertIn('\x1b[7m', frame)
        self.assertNotIn('\x1b[?25h', frame)

    def test_editor_resize_keeps_cursor_visible_and_uses_presence_snapshot(self):
        state = State(time_offset=8)
        state.file = dict(id=2, path='/note')
        state.mode = 'editor'
        state.editor = Editor('中文 👩‍💻\n' * 30)
        state.editor.pos = len(state.editor.text)
        presence = Presence(frozenset({2}), lock_ip='192.0.2.2')
        for size in ((90, 24), (40, 12), (120, 30)):
            renderer = Renderer(state, size, presence)
            frame = renderer.render()
            self.assertIn('locked by 192.0.2.2', renderer.editor_title())
            self.assertIn('\x1b[?25h', frame)
            self.assertGreater(state.editor_top, 0)
            self.assertEqual(state.last_size, size)
        self.assertNotIn('locked by', Renderer(state, (90, 24), Presence()).editor_title())

    def test_prompt_transition_and_render_escape_user_text(self):
        state = State()
        state.ask('file', 'new file: ', '\x1b[2J中文')
        frame = Renderer(state, (40, 12), Presence()).render()
        self.assertIn('new file: ^[[2J中文', frame)
        self.assertIn('\x1b[?25h', frame)
        state.cancel_prompt()
        self.assertEqual(state.mode, 'browser')
        self.assertIsNone(state.prompt_action)
