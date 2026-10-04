import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sshfm.editor import Editor
from sshfm.service import Service
from sshfm.tui import TUI


class MouseTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        process = SimpleNamespace(get_extra_info=lambda _: ('127.0.0.1', 1),
                                  get_terminal_size=lambda: (90, 24, 0, 0))
        self.ui = TUI(process, SimpleNamespace())
        self.ui.entries = [dict(id=i + 2, kind='file') for i in range(40)]

    async def feed(self, data):
        self.ui.parser.feed(data)
        while self.ui.keys:
            await self.ui.handle_key(self.ui.keys.popleft())

    async def test_broadcast_confirmation_keeps_actual_message_for_sender_and_receiver(self):
        ui = self.ui
        ui.service = Service(None)
        receiver = SimpleNamespace(ip='192.0.2.2', status='', redraw=False)
        ui.service.register(ui.owner, ui)
        ui.service.register('receiver', receiver)
        ui.refresh = AsyncMock()
        ui.ask('broadcast', 'broadcast: ', '你好 👋')
        await ui.confirm_prompt()
        expected = f'[{ui.ip}] 你好 👋'
        self.assertEqual(ui.status, expected)
        self.assertEqual(receiver.status, expected)
        self.assertTrue(receiver.redraw)

    async def test_lock_title_uses_live_owner_ip_and_clears_on_release(self):
        ui = self.ui
        ui.file = dict(id=2, path='/hello')
        ui.service.locks = {}
        ui.service.sessions = {'alice': SimpleNamespace(ip='2001:db8::12')}
        self.assertNotIn('locked by', ui.editor_title())
        ui.service.locks[2] = 'alice'
        self.assertIn('locked by 2001:db8::12', ui.editor_title())
        ui.owner = 'alice'
        self.assertIn('locked by 2001:db8::12', ui.editor_title())
        del ui.service.locks[2]
        self.assertNotIn('locked by', ui.editor_title())

    async def test_readable_dates_keep_configured_timezone(self):
        ui = self.ui
        self.assertEqual(ui.format_time(0), '-')
        self.assertEqual(ui.format_time(1_000_000_000), '1970-01-01 00:00')
        ui.time_offset = 8
        self.assertEqual(ui.format_time(1_000_000_000), '1970-01-01 08:00')

    async def test_fragmented_sgr_select_release_and_double_click(self):
        ui = self.ui
        row = ui.layout().top + len(ui.layout().groups)
        ui.open_file = AsyncMock()
        await self.feed('\x1b[<0;2;')
        self.assertEqual(ui.selected, 0)
        await self.feed(f'{row}M')
        self.assertEqual(ui.selected, 1)
        ui.open_file.assert_not_awaited()
        await self.feed(f'\x1b[<0;2;{row}m')
        ui.open_file.assert_not_awaited()
        await self.feed(f'\x1b[<0;2;{row}M')
        ui.open_file.assert_awaited_once_with(3)

    async def test_x10_wrapped_entry_and_wheel_bounds(self):
        ui = self.ui
        ui.process.get_terminal_size = lambda: (40, 12, 0, 0)
        layout = ui.layout()
        # All physical rows of one wrapped entry select the same entry.
        await self.feed('\x1b[M' + ''.join(chr(n + 32) for n in (0, 2, layout.top + 1)))
        self.assertEqual(ui.selected, 0)
        for _ in range(20):
            await self.feed(f'\x1b[<65;2;{layout.top}M')
        self.assertEqual(ui.top, len(ui.entries) - layout.visible)
        self.assertGreaterEqual(ui.selected, ui.top)
        for _ in range(20):
            await self.feed(f'\x1b[<64;2;{layout.top}M')
        self.assertEqual(ui.top, 0)
        self.assertLess(ui.selected, layout.visible)

    async def test_editor_click_preserves_graphemes_and_wraps(self):
        ui = self.ui
        ui.mode = 'editor'
        ui.file = dict(id=2)
        ui.editor = Editor('中é👩‍💻x\nlast')
        row = ui.layout().top
        _, _, numw = ui.metrics()
        await self.feed(f'\x1b[<0;{numw + 2};{row}M')
        self.assertEqual(ui.editor.pos, 0)  # Inside a wide character.
        await self.feed(f'\x1b[<0;{numw + 4};{row}M')
        self.assertEqual(ui.editor.pos, 3)  # After the combining cluster.
        ui.process.get_terminal_size = lambda: (12, 12, 0, 0)
        ui.editor = Editor('abcdefghijk')
        width, _, numw = ui.metrics()
        await self.feed(f'\x1b[<0;{numw + 2};{ui.layout().top + 1}M')
        self.assertEqual(ui.editor.pos, width + 1)

    async def test_editor_wheel_keeps_cursor_visible(self):
        ui = self.ui
        ui.mode = 'editor'
        ui.file = dict(id=2)
        ui.editor = Editor('\n'.join(str(i) for i in range(50)))
        await self.feed(f'\x1b[<65;5;{ui.layout().top}M')
        self.assertEqual(ui.editor_top, 3)
        self.assertEqual(ui.editor.cursor(ui.editor.rows(ui.metrics()[0]), ui.cells)[0], 3)

    async def test_prompt_and_non_action_events_do_not_insert_reports(self):
        ui = self.ui
        ui.ask('file', 'new file: ', '中abc')
        row = ui.size()[1]
        await self.feed(f'\x1b[<0;14;{row}M')
        self.assertEqual(ui.prompt.pos, 2)
        before = ui.prompt.text
        for data in (f'\x1b[<65;2;{row}M', f'\x1b[<2;2;{row}M',
                     f'\x1b[<32;2;{row}M', f'\x1b[<0;999;{row}M'):
            await self.feed(data)
        self.assertEqual(ui.prompt.text, before)
        self.assertEqual(ui.prompt.pos, 2)


if __name__ == '__main__':
    unittest.main()
