import json
from pathlib import Path
import unittest

from sshfm.collation import name_key
from sshfm.editor import CellWidth, Editor
from sshfm.layout import Layout, marquee, size_text


class UpstreamLayoutTests(unittest.TestCase):
    def test_upstream_cpp_layout_at_six_terminal_sizes(self):
        fixtures = json.loads(Path(__file__).with_name('upstream_layout.json').read_text())
        for case in fixtures['cases']:
            with self.subTest(cols=case['cols'], rows=case['rows'], editor=case['editor'], prompt=case['prompt']):
                layout = Layout(case['cols'], case['rows'], case['editor'], case['prompt'], CellWidth())
                actual = dict(top=layout.top, bottom=layout.bottom, keys=layout.keys,
                              groups=layout.groups,
                              widths=[[layout.widths(group)[c] for c in group] for group in layout.groups])
                self.assertEqual(actual, case['expected'])

    def test_name_collation_interleaves_chinese_and_latin_without_directory_bias(self):
        names = ['中', 'z', '波', 'b', '按', 'a', '啊']
        self.assertEqual(sorted(names, key=name_key), ['a', '啊', '按', 'b', '波', 'z', '中'])
        self.assertLess(name_key('a'), name_key('aa'))

    def test_upstream_size_and_exact_width_editor_end(self):
        self.assertEqual(size_text(999999, 6), '999999')
        self.assertEqual(size_text(1000000, 6), '977K')
        editor = Editor('abcd')
        editor.pos = 4
        self.assertEqual(len(editor.rows(4)), 1)
        self.assertEqual(editor.cursor(editor.rows(4)), (0, 4))
        editor.edit('insert', 'e')
        self.assertEqual(editor.cursor(editor.rows(4)), (1, 1))

    def test_marquee_never_splits_graphemes_or_exceeds_width(self):
        cells = CellWidth()
        for offset in range(20):
            shown = marquee('长文件名👩‍💻é中文', offset, 6, cells)
            self.assertEqual(cells(shown), 6)
            self.assertNotIn('\x1b', shown)
            self.assertFalse(shown.startswith('\u200d'))


if __name__ == '__main__':
    unittest.main()
