import unittest
from unittest.mock import patch

import auto_typer


class InputEngineTests(unittest.TestCase):
    def test_newline_normalization_preserves_all_other_characters(self):
        text = "  中文\tEnglish\r\n第二行  \r第三行\n"
        self.assertEqual(
            auto_typer.normalize_newlines(text),
            "  中文\tEnglish\n第二行  \n第三行\n",
        )

    def test_utf16_units_preserve_chinese_and_supplementary_characters(self):
        self.assertEqual(
            auto_typer.utf16_units("A中😀"),
            [0x0041, 0x4E2D, 0xD83D, 0xDE00],
        )

    @patch("auto_typer.send_unicode_character")
    @patch("auto_typer.send_key")
    def test_whitespace_and_characters_use_distinct_input_events(self, send_key, send_unicode):
        text = " A\t中\n"
        with patch("auto_typer.time.sleep"):
            count = auto_typer.type_text(text, 1)
        self.assertEqual(count, len(text))
        self.assertEqual(
            send_unicode.call_args_list,
            [unittest.mock.call("A"), unittest.mock.call("中")],
        )
        self.assertEqual(
            send_key.call_args_list,
            [
                unittest.mock.call(auto_typer.VK_SPACE),
                unittest.mock.call(auto_typer.VK_TAB),
                unittest.mock.call(auto_typer.VK_RETURN),
            ],
        )

    def test_content_counts_distinguish_english_chinese_and_whitespace(self):
        text = "Ab 中文\t\n"
        self.assertEqual(
            auto_typer.content_counts(text),
            {
                "total": 7,
                "english": 2,
                "chinese": 2,
                "spaces": 1,
                "tabs": 1,
                "newlines": 1,
            },
        )

    def test_preview_marks_each_kind_of_whitespace(self):
        self.assertEqual(auto_typer.visible_whitespace("  A\tB\n"), "··A→B↵\n")


if __name__ == "__main__":
    unittest.main()
