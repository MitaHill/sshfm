import unittest

from prompt_toolkit.input.vt100_parser import Vt100Parser

from sshfm.editor import CellWidth, Editor, clip, display, pad


class EditorTests(unittest.TestCase):
    def test_graphemes_are_one_editing_unit(self):
        for text in ('é', '👩‍💻', '🇹🇼', '👍🏽', '❤️'):
            with self.subTest(text=text):
                editor = Editor('a' + text + 'b')
                editor.pos = 1
                editor.move('right', 20, 10)
                self.assertEqual(editor.pos, 1 + len(text))
                editor.edit('backspace')
                self.assertEqual(editor.text, 'ab')
                editor = Editor('a' + text + 'b')
                editor.pos = 1
                editor.edit('delete')
                self.assertEqual(editor.text, 'ab')

    def test_multiline_split_merge(self):
        editor = Editor('first\nlast\n')
        editor.goto(2)
        editor.edit('backspace')
        self.assertEqual(editor.text, 'firstlast\n')
        editor.edit('insert', '\n中文\n')
        self.assertEqual(editor.text, 'first\n中文\nlast\n')

    def test_wrapped_cursor_and_vertical_goal(self):
        editor = Editor('中文abcd\nx\n中文abcd')
        editor.pos = 4  # first visual row is exactly full at six cells
        rows = editor.rows(6)
        self.assertEqual(editor.cursor(rows), (1, 0))
        editor.pos = 3
        editor.move('down', 6, 5)
        editor.move('down', 6, 5)
        self.assertEqual(editor.pos, len('中文abcd\nx'))
        editor.move('down', 6, 5)
        self.assertEqual(editor.pos, len('中文abcd\nx\n中文a'))

    def test_control_bytes_never_escape_into_terminal(self):
        self.assertNotIn('\x1b', display('text\x1b[2J\t\x00\x9b'))
        self.assertEqual(clip('中文x', 3), '中')
        self.assertEqual(pad('👩‍💻', 4), '👩‍💻  ')

    def test_insertion_cannot_leave_cursor_inside_joined_grapheme(self):
        editor = Editor('💻')
        editor.edit('insert', '👩‍')
        self.assertEqual(editor.pos, len('👩‍💻'))
        editor.edit('backspace')
        self.assertEqual(editor.text, '')

    def test_parser_handles_split_sequences_and_bracketed_paste(self):
        keys = []
        parser = Vt100Parser(keys.append)
        parser.feed('\x1b[')
        self.assertEqual(keys, [])
        parser.feed('A')
        self.assertEqual(keys.pop().key.value, 'up')
        parser.feed('\x1b[200~中文\n\x13\x1b[201~')
        event = keys.pop()
        self.assertEqual(event.key.value, '<bracketed-paste>')
        self.assertEqual(event.data, '中文\n\x13')
        parser.feed('\x1b')
        parser.flush()
        self.assertEqual(keys.pop().key.value, 'escape')

    def test_client_width_fallback_keeps_grapheme_editing(self):
        cells = CellWidth()
        self.assertEqual(cells('👩‍💻❤️'), 4)
        cells.zwj = cells.vs16 = False
        self.assertEqual(cells('👩‍💻❤️'), 5)
        editor = Editor('👩‍💻❤️x')
        editor.pos = len('👩‍💻')
        self.assertEqual(editor.cursor(editor.rows(8, cells), cells), (0, 4))
        editor.edit('backspace')
        self.assertEqual(editor.text, '❤️x')


if __name__ == '__main__':
    unittest.main()
