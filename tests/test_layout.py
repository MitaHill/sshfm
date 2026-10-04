import json
from pathlib import Path
import unittest
from unittest.mock import patch

from sshfm.collation import name_key
from sshfm.editor import CellWidth, Editor
from sshfm.layout import Layout, marquee, size_text


class UpstreamLayoutTests(unittest.TestCase):
    @patch('sshfm.layout.CELL_MIN', (18, 6, 12, 12, 15, 15))
    def test_upstream_layout_algorithm_with_original_column_widths(self):
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

    def test_human_readable_sizes(self):
        for size, expected in ((0, '0 B'), (3, '3 B'), (1023, '1023 B'),
                               (1024, '1.0 KiB'), (1536, '1.5 KiB'),
                               (999999, '976.6 KiB'), (1024 ** 2 - 1, '1.0 MiB'),
                               (1024 ** 3, '1.0 GiB')):
            self.assertEqual(size_text(size, 10), expected)
            self.assertLessEqual(len(size_text(size, 8)), 8)

    def test_human_readable_columns_fit_without_clipping(self):
        for cols in (40, 90, 120):
            layout = Layout(cols, 30, False, False, CellWidth())
            for group in layout.groups:
                widths = layout.widths(group)
                for column in group:
                    if column in (2, 3):
                        self.assertGreaterEqual(widths[column], 16)
                    if column == 1:
                        self.assertGreaterEqual(widths[column], 8)

    def test_exact_width_editor_end(self):
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
