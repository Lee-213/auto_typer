import ctypes
import queue
import threading
import unittest
from contextlib import nullcontext
from unittest.mock import Mock, call, patch

import auto_typer
import clipboard_text
import target_text
from clipboard_text import ClipboardSnapshot
from target_text import TargetSnapshot, TargetUnavailable, Verification
from text_parser import compare_text, insert_text, parse_text


CODE_SAMPLE = (
    "def main():\n"
    "    if True:\n"
    "        for i in range(3):\n"
    "            print(i)\n"
    "\n"
    "    value = 1  \n"
    "    break\n"
    "# top-level comment\n"
    'print("Finished")\n'
)


def successful_character_result(char, method=None):
    options = auto_typer.CharacterOptions()
    units = tuple(auto_typer.utf16_units(char))
    requested = 2 * len(units)
    return auto_typer.CharacterSendResult(
        character=char,
        method=method or auto_typer._dispatch_method(char, options),
        utf16_units=units,
        batches=(
            auto_typer.EventBatchResult(
                requested=requested,
                accepted=requested,
                key_down=True,
                key_up=True,
            ),
        ),
    )


class TextParserTests(unittest.TestCase):
    def test_crlf_cr_and_lf_normalize_to_one_logical_newline(self):
        cases = {
            "A\r\nB": "A\nB",
            "A\rB": "A\nB",
            "A\nB": "A\nB",
            "A\r\n\rB\n\nC": "A\n\nB\n\nC",
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                parsed = parse_text(source)
                self.assertEqual(parsed.normalized_text, expected)
                self.assertEqual(parsed.reconstruct(), expected)
                self.assertEqual(parsed.newline_count, expected.count("\n"))

    def test_normalization_preserves_every_other_character(self):
        source = "  中文\tEnglish😀!\r\n第二行  \r第三行\n"
        parsed = parse_text(source)
        self.assertEqual(parsed.original_text, source)
        self.assertEqual(parsed.normalized_text, "  中文\tEnglish😀!\n第二行  \n第三行\n")
        self.assertEqual(parsed.reconstruct(), parsed.normalized_text)

    def test_required_sample_reports_exact_line_indentation(self):
        parsed = parse_text('def main():\n    if True:\n        print("Hello")')
        self.assertEqual(
            [(line.leading_spaces, line.leading_tabs) for line in parsed.lines],
            [(0, 0), (4, 0), (8, 0)],
        )
        self.assertEqual(
            [line.content for line in parsed.lines],
            ['def main():', '    if True:', '        print("Hello")'],
        )

    def test_zero_one_two_four_eight_twelve_space_lines(self):
        widths = [0, 1, 2, 4, 8, 12]
        source = "\n".join(" " * width + f"L{width}" for width in widths)
        parsed = parse_text(source)
        self.assertEqual([line.leading_spaces for line in parsed.lines], widths)
        self.assertEqual([line.leading_tabs for line in parsed.lines], [0] * len(widths))
        self.assertEqual(parsed.reconstruct(), source)

    def test_ten_consecutive_four_space_lines(self):
        source = "\n".join("    value = 1" for _ in range(10))
        parsed = parse_text(source)
        self.assertEqual(parsed.line_count, 10)
        self.assertEqual([line.leading_spaces for line in parsed.lines], [4] * 10)
        self.assertEqual(parsed.reconstruct(), source)

    def test_progressively_nested_indentation(self):
        source = "\n".join(" " * depth + f"level{depth}" for depth in (0, 1, 2, 4, 8, 12))
        parsed = parse_text(source)
        self.assertEqual([line.leading_spaces for line in parsed.lines], [0, 1, 2, 4, 8, 12])
        self.assertEqual(parsed.reconstruct(), source)

    def test_mixed_space_tab_order_and_inner_tabs(self):
        source = " \t  \tA \t B  \n\t \t\n"
        parsed = parse_text(source)
        first, second, last = parsed.lines
        self.assertEqual(first.leading_whitespace, " \t  \t")
        self.assertEqual((first.leading_spaces, first.leading_tabs), (3, 2))
        self.assertEqual(first.leading_whitespace_count, 5)
        self.assertEqual(first.inner_tabs, 1)
        self.assertEqual(first.trailing_spaces, 2)
        self.assertEqual((second.leading_spaces, second.leading_tabs), (1, 2))
        self.assertEqual(second.inner_tabs, 0)
        self.assertTrue(second.is_blank)
        self.assertTrue(last.is_blank)
        self.assertEqual(parsed.reconstruct(), source)

    def test_leading_middle_trailing_and_consecutive_spaces(self):
        source = "  A  B   \n C \n"
        parsed = parse_text(source)
        self.assertEqual(parsed.lines[0].leading_spaces, 2)
        self.assertEqual(parsed.lines[0].trailing_spaces, 3)
        self.assertEqual(parsed.lines[1].content, " C ")
        self.assertEqual(parsed.reconstruct(), source)

    def test_consecutive_blank_lines_and_final_newline_state(self):
        cases = ["", "A", "A\n", "A\n\n", "\n\n\n", "   \n\t\n\n"]
        for source in cases:
            with self.subTest(source=source):
                parsed = parse_text(source)
                self.assertEqual(parsed.reconstruct(), source)
                self.assertEqual(parsed.ends_with_newline, source.endswith("\n"))
                self.assertEqual(parsed.line_count, source.count("\n") + 1 if source else 0)

    def test_last_line_and_terminators_are_explicit(self):
        parsed = parse_text("A\n\n")
        self.assertEqual([line.terminator for line in parsed.lines], ["\n", "\n", ""])
        self.assertEqual([line.is_last_line for line in parsed.lines], [False, False, True])
        self.assertEqual([line.number for line in parsed.lines], [1, 2, 3])

    def test_chinese_english_emoji_and_punctuation(self):
        source = "中文 English 😀 !?\t\n"
        parsed = parse_text(source)
        self.assertEqual(parsed.reconstruct(), source)
        self.assertEqual(parsed.lines[0].content, "中文 English 😀 !?\t")
        self.assertEqual(parsed.tabs, 1)

    def test_complete_python_function_and_nested_control_structures(self):
        parsed = parse_text(CODE_SAMPLE)
        self.assertEqual(parsed.reconstruct(), CODE_SAMPLE)
        self.assertEqual(parsed.line_count, 10)
        self.assertEqual(
            [line.leading_spaces for line in parsed.lines],
            [0, 4, 8, 12, 0, 4, 4, 0, 0, 0],
        )
        self.assertEqual(parsed.lines[5].trailing_spaces, 2)
        self.assertEqual(parsed.lines[7].content, "# top-level comment")
        self.assertTrue(parsed.ends_with_newline)

    def test_empty_space_only_and_tab_only_lines(self):
        empty = parse_text("")
        self.assertEqual(empty.lines, ())
        self.assertEqual(empty.sha256, parse_text("").sha256)

        spaces = parse_text("   ")
        self.assertEqual(spaces.line_count, 1)
        self.assertEqual(spaces.lines[0].leading_spaces, 3)
        self.assertEqual(spaces.lines[0].trailing_spaces, 3)
        self.assertTrue(spaces.lines[0].is_blank)

        tabs = parse_text("\t")
        self.assertEqual(tabs.lines[0].leading_tabs, 1)
        self.assertEqual(tabs.lines[0].inner_tabs, 0)
        self.assertEqual(tabs.lines[0].trailing_spaces, 0)

    def test_max_consecutive_blank_lines_counts_space_and_tab_lines(self):
        parsed = parse_text("A\n \n\t\n\nB\n\n")
        self.assertEqual(parsed.max_consecutive_blank_lines, 3)
        self.assertEqual(parsed.as_dict()["max_consecutive_blank_lines"], 3)
        self.assertEqual(parsed.reconstruct(), "A\n \n\t\n\nB\n\n")

    def test_character_line_numbers_match_every_character(self):
        parsed = parse_text(" A\n\n\tB\n")
        numbers = parsed.character_line_numbers()
        self.assertEqual(len(numbers), len(parsed.normalized_text))
        self.assertEqual(numbers, (1, 1, 1, 2, 3, 3, 3))

    def test_reconstruction_corpus_is_lossless(self):
        corpus = [
            "",
            " ",
            "\t",
            "\n",
            "\r",
            "\r\n",
            "A\r\nB\rC\n",
            " \t  \tA \t B  \n\t \t\n",
            CODE_SAMPLE,
            "中文😀\r\n\t\r",
            "A  \n\n\nB",
        ]
        for source in corpus:
            with self.subTest(source=source):
                parsed = parse_text(source)
                self.assertEqual(parsed.reconstruct(), auto_typer.normalize_newlines(source))

    def test_line_analysis_uses_repr_and_exact_counts(self):
        text = auto_typer.format_line_analysis(auto_typer.analyze_text(" \tA  \nB"))
        self.assertIn("原文 SHA-256", text)
        self.assertIn("第1行", text)
        self.assertIn("行首空格 1", text)
        self.assertIn("行首Tab 1", text)
        self.assertIn("行尾空格 2", text)
        self.assertIn("行内Tab 0", text)
        self.assertIn("' \\tA  '", text)


class TextComparisonTests(unittest.TestCase):
    def test_compare_text_detects_per_line_format_differences(self):
        expected = " A\t\n\nB  \n"
        actual = "  A\t\n\nB \n"
        result = compare_text(expected, actual)
        self.assertEqual(result["status"], "mismatch")
        self.assertEqual(result["difference_count"], 2)
        self.assertEqual(result["expected_spaces"], 3)
        self.assertEqual(result["actual_spaces"], 3)
        self.assertEqual(result["expected_tabs"], result["actual_tabs"])
        self.assertEqual(result["expected_newlines"], result["actual_newlines"])
        self.assertTrue(result["expected_ends_with_newline"])
        self.assertEqual([item["line_number"] for item in result["differences"]], [1, 3])

    def test_compare_text_allows_only_newline_representation_differences(self):
        result = compare_text("A\r\nB\r", "A\nB\n")
        self.assertEqual(result["status"], "verified")
        self.assertTrue(result["exact_match"])

    def test_insert_text_preserves_prefix_suffix_and_selection_replacement(self):
        self.assertEqual(insert_text("prefixsuffix", " \t中\n", 6), "prefix \t中\nsuffix")
        self.assertEqual(insert_text("abcdef", "X", 1, 4), "aXef")
        with self.assertRaises(ValueError):
            insert_text("abc", "X", 4)

    def test_normalized_offset_maps_to_raw_crlf_and_cr_positions(self):
        raw = "A\r\nB\rC\n"
        self.assertEqual(
            [target_text._raw_offset_from_normalized(raw, offset) for offset in range(7)],
            [0, 1, 3, 4, 5, 6, 7],
        )
        with self.assertRaises(ValueError):
            target_text._raw_offset_from_normalized(raw, -1)
        with self.assertRaises(ValueError):
            target_text._raw_offset_from_normalized(raw, 7)

    def test_analyze_text_retains_v2_fields_and_adds_parser_details(self):
        analysis = auto_typer.analyze_text("Ab 中文😀\t\r\n")
        self.assertEqual(analysis["normalized_text"], "Ab 中文😀\t\n")
        self.assertEqual(analysis["total"], 8)
        self.assertEqual(analysis["line_count"], 2)
        self.assertEqual(analysis["english"], 2)
        self.assertEqual(analysis["chinese"], 2)
        self.assertEqual(analysis["spaces"], 1)
        self.assertEqual(analysis["tabs"], 1)
        self.assertEqual(analysis["newlines"], 1)
        self.assertEqual(analysis["line_indents"], [{"spaces": 0, "tabs": 0, "characters": 0}, {"spaces": 0, "tabs": 0, "characters": 0}])
        self.assertEqual(analysis["trailing_spaces"], [0, 0])
        self.assertEqual(len(analysis["sha256"]), 64)


class CharacterDispatchTests(unittest.TestCase):
    @patch("auto_typer.send_unicode_character")
    @patch("auto_typer.send_key")
    def test_unicode_mode_keeps_space_and_tab_as_distinct_characters(self, send_key, send_unicode):
        for char in (" ", "\t", "\n", "\r", "中"):
            auto_typer.send_character(char)
        self.assertEqual(
            send_key.call_args_list,
            [
                call(auto_typer.VK_SPACE),
                call(auto_typer.VK_TAB),
                call(auto_typer.VK_RETURN),
                call(auto_typer.VK_RETURN),
            ],
        )
        self.assertEqual(send_unicode.call_args_list, [call("中")])

    @patch("auto_typer.send_unicode_character")
    @patch("auto_typer.send_key")
    def test_compatibility_mode_uses_virtual_whitespace_keys(self, send_key, send_unicode):
        auto_typer.send_character(" ", literal_mode=False)
        auto_typer.send_character("\t", literal_mode=False)
        auto_typer.send_character("\n", literal_mode=False)
        auto_typer.send_character("A", literal_mode=False)
        self.assertEqual(
            send_key.call_args_list,
            [call(auto_typer.VK_SPACE), call(auto_typer.VK_TAB), call(auto_typer.VK_RETURN)],
        )
        self.assertEqual(send_unicode.call_args_list, [call("A")])

    def test_utf16_units_preserve_chinese_and_supplementary_characters(self):
        self.assertEqual(auto_typer.utf16_units("A中😀"), [0x0041, 0x4E2D, 0xD83D, 0xDE00])

    @patch("auto_typer._send_input")
    @patch("auto_typer._keyboard_event")
    def test_send_key_emits_key_down_then_key_up(self, keyboard_event, send_input):
        keyboard_event.side_effect = lambda vk, scan, flags: (vk, scan, flags)
        auto_typer.send_key(auto_typer.VK_TAB)
        self.assertEqual(
            keyboard_event.call_args_list,
            [call(auto_typer.VK_TAB, 0, 0), call(auto_typer.VK_TAB, 0, auto_typer.KEYEVENTF_KEYUP)],
        )
        send_input.assert_called_once_with(
            ((auto_typer.VK_TAB, 0, 0), (auto_typer.VK_TAB, 0, auto_typer.KEYEVENTF_KEYUP))
        )

    @patch("auto_typer._send_input")
    @patch("auto_typer._keyboard_event")
    def test_paste_shortcut_is_one_control_v_chord(self, keyboard_event, send_input):
        keyboard_event.side_effect = lambda vk, scan, flags: (vk, scan, flags)
        auto_typer.send_paste_shortcut()
        send_input.assert_called_once_with(
            (
                (auto_typer.VK_CONTROL, 0, auto_typer.KEYEVENTF_KEYUP),
                (auto_typer.VK_V, 0, auto_typer.KEYEVENTF_KEYUP),
                (auto_typer.VK_CONTROL, 0, 0),
                (auto_typer.VK_V, 0, 0),
                (auto_typer.VK_V, 0, auto_typer.KEYEVENTF_KEYUP),
                (auto_typer.VK_CONTROL, 0, auto_typer.KEYEVENTF_KEYUP),
            )
        )
        self.assertNotIn(auto_typer.VK_RETURN, [event[0] for event in send_input.call_args.args[0]])

    @patch("auto_typer._send_input")
    @patch("auto_typer._keyboard_event")
    def test_unicode_character_emits_each_utf16_unit_down_and_up(self, keyboard_event, send_input):
        keyboard_event.side_effect = lambda vk, scan, flags: (vk, scan, flags)
        auto_typer.send_unicode_character("😀")
        expected_events = (
            (0, 0xD83D, auto_typer.KEYEVENTF_UNICODE),
            (0, 0xD83D, auto_typer.KEYEVENTF_UNICODE | auto_typer.KEYEVENTF_KEYUP),
            (0, 0xDE00, auto_typer.KEYEVENTF_UNICODE),
            (0, 0xDE00, auto_typer.KEYEVENTF_UNICODE | auto_typer.KEYEVENTF_KEYUP),
        )
        self.assertEqual(keyboard_event.call_args_list, [call(*event) for event in expected_events])
        send_input.assert_called_once_with(expected_events)

    @patch("auto_typer.send_printable_character")
    @patch("auto_typer.send_newline")
    @patch("auto_typer.send_tab")
    @patch("auto_typer.send_space")
    def test_default_character_modes_are_independent(self, send_space, send_tab, send_newline, send_printable):
        for char in (" ", "\t", "\n", "中"):
            auto_typer.send_character(char)
        send_space.assert_called_once_with(auto_typer.SPACE_MODE_VIRTUAL)
        send_tab.assert_called_once_with(auto_typer.TAB_MODE_VIRTUAL)
        send_newline.assert_called_once_with(auto_typer.NEWLINE_MODE_ENTER)
        send_printable.assert_called_once_with("中")

    @patch("auto_typer.send_unicode_character", side_effect=lambda char: successful_character_result(char, "unicode"))
    @patch("auto_typer.send_key")
    def test_explicit_character_modes_override_each_other(self, send_key, send_unicode):
        send_key.return_value = auto_typer.EventBatchResult(2, 2, True, True)
        options = auto_typer.CharacterOptions(
            auto_typer.SPACE_MODE_VIRTUAL,
            auto_typer.TAB_MODE_UNICODE,
            auto_typer.NEWLINE_MODE_UNICODE_LF,
        )
        results = [
            auto_typer.send_character(" ", options=options),
            auto_typer.send_character("\t", options=options),
            auto_typer.send_character("\n", options=options),
        ]
        self.assertEqual([result.method for result in results], ["vk_space", "unicode_tab", "unicode_lf"])
        send_key.assert_called_once_with(auto_typer.VK_SPACE)
        self.assertEqual(send_unicode.call_args_list, [call("\t"), call("\n")])

    @patch("auto_typer._send_input", return_value=4)
    def test_character_result_reports_all_utf16_events(self, send_input):
        result = auto_typer.send_unicode_character("😀")
        self.assertEqual((result.requested, result.accepted), (4, 4))
        self.assertTrue(result.key_down_success)
        self.assertTrue(result.key_up_success)
        self.assertEqual(result.utf16_units, (0xD83D, 0xDE00))
        send_input.assert_called_once()

    def test_character_event_record_contains_required_diagnostics(self):
        result = auto_typer.CharacterSendResult(
            "😀",
            "unicode",
            (0xD83D, 0xDE00),
            (auto_typer.EventBatchResult(4, 3, False, False, True, "partial"),),
        )
        record = auto_typer._character_event_record(7, "😀", result, "unicode", "failed", "partial")
        data = record.as_dict()
        self.assertEqual(data["position"], 7)
        self.assertEqual(data["character_repr"], repr("😀"))
        self.assertEqual(data["codepoints"], ["U+1F600"])
        self.assertEqual(data["utf16_units"], ["0xD83D", "0xDE00"])
        self.assertEqual((data["requested"], data["accepted"]), (4, 3))
        self.assertFalse(data["key_down_success"])
        self.assertFalse(data["key_up_success"])
        self.assertEqual(data["kind"], "printable")
        self.assertIn("请求事件=4", auto_typer.format_character_event(record))

    def test_invalid_character_modes_are_rejected(self):
        with self.assertRaises(ValueError):
            auto_typer.send_space("invalid")
        with self.assertRaises(ValueError):
            auto_typer.send_tab("invalid")
        with self.assertRaises(ValueError):
            auto_typer.send_newline("invalid")

    @patch("auto_typer.IS_WINDOWS", False)
    def test_non_windows_calls_raise_clear_error_instead_of_name_error(self):
        with self.assertRaisesRegex(OSError, "文本分析和单元测试仍可使用"):
            auto_typer.send_character("A")
        with self.assertRaisesRegex(OSError, "文本分析和单元测试仍可使用"):
            auto_typer.is_f8_pressed()

    @patch("auto_typer.IS_WINDOWS", True)
    @patch("auto_typer.WINDOWS_API_ERROR", "user32 初始化失败")
    def test_windows_api_initialization_error_is_reported(self):
        with self.assertRaisesRegex(OSError, "user32 初始化失败"):
            auto_typer.send_key(auto_typer.VK_RETURN)


class TypeTextTests(unittest.TestCase):
    def record_typing(self, text, literal_mode=True, stop_event=None, should_stop=None):
        received = []
        events = []
        states = []
        key_characters = {
            auto_typer.VK_TAB: "\t",
            auto_typer.VK_RETURN: "\n",
            auto_typer.VK_SPACE: " ",
        }

        def record_key(virtual_key):
            events.append(("key", virtual_key))
            received.append(key_characters[virtual_key])

        def record_unicode(char):
            events.append(("unicode", char))
            received.append(char)
            return successful_character_result(char, "unicode")

        with (
            patch("auto_typer.send_key", side_effect=record_key),
            patch("auto_typer.send_unicode_character", side_effect=record_unicode),
            patch("auto_typer._send_input", side_effect=AssertionError("单元测试不得发送系统键盘事件")),
            patch("auto_typer.time.sleep"),
        ):
            count = auto_typer.type_text(
                text,
                0,
                stop_event=stop_event,
                should_stop=should_stop,
                literal_mode=literal_mode,
                on_state=states.append,
            )
        return count, "".join(received), events, states

    def test_event_sequence_and_counts_in_unicode_mode(self):
        source = "A \t中\r\n\nB"
        count, received, events, states = self.record_typing(source)
        self.assertEqual(count, 7)
        self.assertEqual(received, "A \t中\n\nB")
        self.assertEqual(
            events,
            [
                ("unicode", "A"),
                ("unicode", " "),
                ("unicode", "\t"),
                ("unicode", "中"),
                ("key", auto_typer.VK_RETURN),
                ("key", auto_typer.VK_RETURN),
                ("unicode", "B"),
            ],
        )
        final = states[-1]
        self.assertEqual((final.sent_spaces, final.sent_tabs, final.sent_enters), (1, 1, 2))
        self.assertEqual(final.sent_unicode_characters, 3)
        self.assertEqual(final.position, 7)

    def test_event_sequence_in_compatibility_mode(self):
        count, received, events, _states = self.record_typing("A \t中\n", literal_mode=False)
        self.assertEqual(count, 5)
        self.assertEqual(received, "A \t中\n")
        self.assertEqual(
            events,
            [
                ("unicode", "A"),
                ("key", auto_typer.VK_SPACE),
                ("key", auto_typer.VK_TAB),
                ("unicode", "中"),
                ("key", auto_typer.VK_RETURN),
            ],
        )

    def test_full_code_sample_replays_exactly_in_both_modes(self):
        for literal_mode in (True, False):
            with self.subTest(literal_mode=literal_mode):
                count, received, events, states = self.record_typing(CODE_SAMPLE, literal_mode)
                self.assertEqual(count, len(CODE_SAMPLE))
                self.assertEqual(received, CODE_SAMPLE)
                self.assertEqual(states[-1].sent_enters, CODE_SAMPLE.count("\n"))
                self.assertEqual(states[-1].sent_tabs, CODE_SAMPLE.count("\t"))
                self.assertEqual(
                    sum(event == ("key", auto_typer.VK_RETURN) for event in events),
                    CODE_SAMPLE.count("\n"),
                )

    def test_required_whitespace_corpus_replays_exactly(self):
        cases = [
            "\n".join(" " * width + f"L{width}" for width in (0, 1, 2, 4, 8, 12)),
            "\n".join("    value = 1" for _ in range(10)),
            " \t  \tA \t B  \n\t \t\n",
            "A  \n\n\nB",
            "A\n",
            "A",
            "中文 English 😀\r\n\t\r",
            CODE_SAMPLE,
        ]
        for literal_mode in (True, False):
            for source in cases:
                with self.subTest(source=source, literal_mode=literal_mode):
                    count, received, _events, _states = self.record_typing(source, literal_mode)
                    self.assertEqual(count, len(auto_typer.normalize_newlines(source)))
                    self.assertEqual(received, auto_typer.normalize_newlines(source))

    def test_empty_text_sends_nothing(self):
        count, received, events, states = self.record_typing("")
        self.assertEqual((count, received, events), (0, "", []))
        self.assertEqual(len(states), 1)
        self.assertEqual(states[0].position, 0)

    def test_newlines_never_generate_cleanup_keys(self):
        source = "if True:\n    break\n# comment\n"
        count, received, events, _states = self.record_typing(source)
        self.assertEqual(count, len(source))
        self.assertEqual(received, source)
        self.assertEqual([event for event in events if event[0] == "key"], [("key", auto_typer.VK_RETURN)] * 3)
        self.assertEqual(events.count(("unicode", " ")), 6)

    def test_state_tracks_current_line_original_indent_and_repr(self):
        source = " \t中 \n\tB\n"
        _count, _received, _events, states = self.record_typing(source)
        after_enter = next(state for state in states if state.position == 5)
        self.assertEqual(after_enter.line_number, 2)
        self.assertEqual(after_enter.line_content_repr, repr("\tB"))
        self.assertEqual((after_enter.leading_spaces, after_enter.leading_tabs), (0, 1))
        self.assertEqual(after_enter.last_character_repr, repr("\n"))
        final = states[-1]
        self.assertEqual((final.sent_spaces, final.sent_tabs, final.sent_enters), (2, 2, 2))
        self.assertIn("\\t", auto_typer.format_input_state(after_enter))

    def test_final_newline_moves_state_to_final_empty_logical_line(self):
        _count, _received, _events, states = self.record_typing("A\n")
        final = states[-1]
        self.assertEqual(final.position, 2)
        self.assertEqual(final.line_number, 2)
        self.assertEqual(final.line_content_repr, "''")
        self.assertEqual(final.last_character_repr, repr("\n"))

    @patch("auto_typer.send_character")
    def test_progress_uses_normalized_character_positions(self, send_character):
        progress = []
        states = []
        with patch("auto_typer.time.sleep"):
            count = auto_typer.type_text("A\r\nB", 0, on_progress=progress.append, on_state=states.append)
        self.assertEqual(count, 3)
        self.assertEqual(progress, [1, 2, 3])
        self.assertEqual([state.position for state in states], [0, 1, 2, 3])
        self.assertEqual(send_character.call_args_list, [call("A"), call("\n"), call("B")])

    @patch("auto_typer.send_character")
    def test_stop_event_before_start_sends_nothing(self, send_character):
        stop_event = threading.Event()
        stop_event.set()
        self.assertEqual(auto_typer.type_text("ABC", 0, stop_event=stop_event), 0)
        send_character.assert_not_called()

    @patch("auto_typer.send_character")
    def test_stop_event_after_first_character_preserves_progress(self, send_character):
        stop_event = threading.Event()
        states = []
        send_character.side_effect = lambda _char, *_args, **_kwargs: (
            stop_event.set(), successful_character_result(_char)
        )[1]
        count = auto_typer.type_text("ABC", 0, stop_event=stop_event, on_state=states.append)
        self.assertEqual(count, 1)
        self.assertEqual(states[-1].position, 1)
        send_character.assert_called_once_with("A")

    @patch("auto_typer.send_character")
    def test_global_stop_callback_interrupts_typing(self, send_character):
        stop_checks = iter([False, True])
        count = auto_typer.type_text("ABC", 0, should_stop=lambda: next(stop_checks))
        self.assertEqual(count, 1)
        send_character.assert_called_once_with("A")

    @patch("auto_typer.send_character")
    def test_stop_during_newline_settle_prevents_next_line(self, send_character):
        stop_event = threading.Event()
        with patch.object(stop_event, "wait", return_value=True):
            count = auto_typer.type_text("\nB", 0, stop_event=stop_event)
        self.assertEqual(count, 1)
        send_character.assert_called_once_with("\n")

    @patch("auto_typer.send_character")
    def test_newline_waits_once_without_sending_extra_keys(self, send_character):
        with patch("auto_typer.time.sleep") as sleep:
            auto_typer.type_text("A\nB", 0)
        sleep.assert_called_once_with(0.05)
        self.assertEqual(send_character.call_args_list, [call("A"), call("\n"), call("B")])

    @patch("auto_typer.send_character", side_effect=successful_character_result)
    def test_default_modes_emit_ordered_character_diagnostics(self, _send_character):
        records = []
        progress = []
        with patch("auto_typer.time.sleep"):
            count = auto_typer.type_text(
                " \t\n\n",
                0,
                on_event=records.append,
                on_progress=lambda value: progress.append((value, len(records))),
            )
        self.assertEqual(count, 4)
        self.assertEqual([record.character_repr for record in records], ["' '", "'\\t'", "'\\n'", "'\\n'"])
        self.assertEqual([record.method for record in records], ["vk_space", "vk_tab", "vk_return", "vk_return"])
        self.assertEqual([record.kind for record in records], ["space", "tab", "newline", "newline"])
        self.assertEqual(progress, [(1, 1), (2, 2), (3, 3), (4, 4)])
        self.assertTrue(all(record.accepted == record.requested for record in records))

    @patch("auto_typer.send_character")
    def test_independent_modes_are_forwarded_without_legacy_override(self, send_character):
        options = auto_typer.CharacterOptions(
            auto_typer.SPACE_MODE_VIRTUAL,
            auto_typer.TAB_MODE_UNICODE,
            auto_typer.NEWLINE_MODE_UNICODE_LF,
        )
        auto_typer.type_text(
            " \t\n",
            0,
            space_mode=options.space_mode,
            tab_mode=options.tab_mode,
            newline_mode=options.newline_mode,
        )
        self.assertEqual(send_character.call_args_list, [call(char, options=options) for char in " \t\n"])

    @patch("auto_typer.send_character")
    def test_failed_character_is_logged_and_never_counted(self, send_character):
        records = []
        progress = []
        partial = auto_typer.EventBatchResult(2, 1, True, False, True, "partial")

        def send(char):
            if char == "B":
                raise auto_typer.SendInputError("partial", partial, 5)
            return successful_character_result(char)

        send_character.side_effect = send
        with self.assertRaises(auto_typer.SendInputError):
            auto_typer.type_text("AB", 0, on_event=records.append, on_progress=progress.append)
        self.assertEqual(progress, [1])
        self.assertEqual([record.status for record in records], ["sent", "failed"])
        self.assertEqual(records[-1].position, 2)
        self.assertEqual((records[-1].requested, records[-1].accepted), (2, 1))
        self.assertTrue(records[-1].key_down_success)
        self.assertFalse(records[-1].key_up_success)

    @patch("auto_typer.send_character", side_effect=successful_character_result)
    def test_consecutive_spaces_newlines_and_tabs_keep_source_order(self, send_character):
        records = []
        with patch("auto_typer.time.sleep"):
            count = auto_typer.type_text("  \n\n\t\t", 0, on_event=records.append)
        self.assertEqual(count, 6)
        self.assertEqual(send_character.call_args_list, [call(char) for char in "  \n\n\t\t"])
        self.assertEqual([record.character_repr for record in records], [repr(char) for char in "  \n\n\t\t"])

    @patch("auto_typer.send_character", side_effect=successful_character_result)
    def test_surrogate_pair_is_one_completed_character(self, send_character):
        records = []
        count = auto_typer.type_text("😀", 0, on_event=records.append)
        self.assertEqual(count, 1)
        self.assertEqual(records[0].utf16_units, (0xD83D, 0xDE00))
        self.assertEqual((records[0].requested, records[0].accepted), (4, 4))
        send_character.assert_called_once_with("😀")

    @patch("auto_typer.send_character")
    def test_concurrent_input_engine_is_rejected(self, send_character):
        acquired = threading.Event()
        release = threading.Event()

        def hold_lock():
            auto_typer._INPUT_ENGINE_LOCK.acquire()
            try:
                acquired.set()
                release.wait(2)
            finally:
                auto_typer._INPUT_ENGINE_LOCK.release()

        holder = threading.Thread(target=hold_lock)
        holder.start()
        try:
            self.assertTrue(acquired.wait(1))
            with self.assertRaisesRegex(RuntimeError, "已有输入任务"):
                auto_typer.type_text("A", 0)
            send_character.assert_not_called()
        finally:
            release.set()
            holder.join(2)


class TargetCursorTests(unittest.TestCase):
    def test_empty_target_can_be_fully_verified_without_selection_offsets(self):
        controller = target_text.TargetController()
        controller._snapshot = TargetSnapshot(
            readable=True,
            text="",
            start=None,
            end=None,
            direct_writable=False,
            reason="no TextPattern selection",
        )
        self.assertEqual(controller.expected_text("A\n"), "A\n")
        controller._snapshot = TargetSnapshot(
            readable=True,
            text="existing",
            start=None,
            end=None,
            direct_writable=False,
            reason="unknown selection",
        )
        self.assertIsNone(controller.expected_text("A"))

    def test_verify_records_clipboard_method_and_normalized_match(self):
        controller = target_text.TargetController()
        controller._snapshot = TargetSnapshot(
            readable=True,
            text="",
            start=0,
            end=0,
            direct_writable=False,
            reason="test",
        )
        current = TargetSnapshot(
            readable=True,
            text="A\r\nB",
            start=3,
            end=3,
            direct_writable=False,
            reason="test",
        )
        with (
            patch.object(controller, "_read_current", return_value=current),
            patch.object(controller, "_same_focus", return_value=True),
        ):
            result = controller.verify("A\nB", method="clipboard")
        self.assertEqual(result.status, "verified")
        self.assertEqual(result.method, "clipboard")
        self.assertTrue(result.exact_match)

    def test_direct_write_maps_lf_insertion_to_crlf_target_caret(self):
        controller = target_text.TargetController()
        controller._snapshot = TargetSnapshot(
            readable=True,
            text="",
            start=0,
            end=0,
            direct_writable=True,
            reason="test",
        )
        before = TargetSnapshot(
            readable=True,
            text="",
            start=0,
            end=0,
            direct_writable=True,
            reason="test",
        )
        after = TargetSnapshot(
            readable=True,
            text="A\r\nB",
            start=4,
            end=4,
            direct_writable=True,
            reason="test",
        )
        with (
            patch.object(controller, "_read_current", side_effect=[before, after]),
            patch.object(controller, "_pattern", return_value=1),
            patch.object(controller, "_restore_caret", return_value=True) as restore,
            patch.object(controller, "_same_focus", return_value=True),
            patch("target_text._method", return_value=lambda *args: 0),
            patch("target_text._release"),
            patch("target_text.oleaut32", create=True) as oleaut32,
            patch("target_text.time.sleep"),
        ):
            oleaut32.SysAllocString.return_value = 123
            result = controller.write_exact("A\nB")
        self.assertEqual(result.status, "verified")
        restore.assert_called_once_with(4)

    def test_cursor_restore_requires_readback_at_requested_raw_offset(self):
        controller = target_text.TargetController()
        moves = []

        def fake_method(_pointer, index, _restype, *_argtypes):
            if index == 14:
                def move(_this, *args):
                    moves.append(args)
                    return 0
                return move
            if index == 16:
                return lambda _this: 0
            raise AssertionError(f"unexpected vtable index {index}")

        with (
            patch.object(controller, "_document_range", return_value=10),
            patch.object(controller, "_range_clone", return_value=20),
            patch.object(controller, "_range_text", return_value="A\r\nB"),
            patch.object(controller, "_selection_offsets", side_effect=[(4, 4), (2, 2)]),
            patch("target_text._method", side_effect=fake_method),
            patch("target_text._release"),
        ):
            self.assertTrue(controller._restore_caret(4))
            self.assertFalse(controller._restore_caret(3))
        self.assertEqual([move[:3] for move in moves], [(1, 0, 0), (1, 0, -1)])


class FakeTarget:
    def __init__(
        self,
        text="",
        start=0,
        end=0,
        direct=False,
        write_status="verified",
        readable=True,
        dynamic_capture=False,
    ):
        self.snapshot = TargetSnapshot(
            readable=readable,
            text=text if readable else None,
            start=start if readable else None,
            end=end if readable else None,
            direct_writable=direct,
            reason="fake target",
            value_pattern_supported=direct,
            text_pattern_supported=readable,
            value_read_only=False if direct else None,
        )
        self.text = text
        self.write_status = write_status
        self.writes = []
        self.closed = False
        self.write_attempted = False
        self.fail_write = False
        self.dynamic_capture = dynamic_capture
        self.capture_calls = 0

    def capture(self):
        self.capture_calls += 1
        if not self.dynamic_capture:
            return self.snapshot
        snapshot = TargetSnapshot(
            readable=self.snapshot.readable,
            text=self.text if self.snapshot.readable else None,
            start=len(self.text) if self.snapshot.readable else None,
            end=len(self.text) if self.snapshot.readable else None,
            direct_writable=False,
            reason="dynamic fake target",
            value_pattern_supported=self.snapshot.value_pattern_supported,
            text_pattern_supported=self.snapshot.text_pattern_supported,
            value_read_only=self.snapshot.value_read_only,
        )
        self.snapshot = snapshot
        return snapshot

    def expected_text(self, insertion):
        snapshot = self.snapshot
        if snapshot.text is None:
            return None
        if snapshot.start is None or snapshot.end is None:
            return insertion if snapshot.text == "" else None
        return snapshot.text[: snapshot.start] + insertion + snapshot.text[snapshot.end :]

    def write_exact(self, insertion):
        self.write_attempted = True
        if self.fail_write:
            raise OSError("fake write failure")
        expected = self.expected_text(insertion)
        self.writes.append(expected)
        self.text = expected
        comparison = compare_text(expected, self.text)
        return Verification(
            status=self.write_status,
            method="uia_value",
            reason="fake verification",
            readable=True,
            exact_match=comparison["exact_match"],
            focus_same=True,
            caret_restored=True,
            comparison=comparison,
        )

    def verify(self, expected, method="keyboard"):
        comparison = compare_text(expected, self.text)
        return Verification(
            status="verified" if comparison["exact_match"] else "mismatch",
            method=method,
            reason="fake verification",
            readable=True,
            exact_match=comparison["exact_match"],
            focus_same=True,
            comparison=comparison,
        )

    def close(self):
        self.closed = True


class FakeClipboard:
    def __init__(self, text="previous clipboard"):
        self.text = text
        self.sequence = 1
        self.writes = []
        self.restores = []
        self.reads = 0
        self.fail_write = False
        self.read_override = None
        self.on_write = None

    def capture(self):
        return ClipboardSnapshot(self.text, self.sequence)

    def write(self, text):
        if self.fail_write:
            raise OSError("fake clipboard write failure")
        self.text = text
        self.sequence += 1
        self.writes.append(text)
        if self.on_write is not None:
            self.on_write()
        return self.sequence

    def read(self):
        self.reads += 1
        return self.text if self.read_override is None else self.read_override

    def restore(self, snapshot, expected_sequence):
        self.restores.append((snapshot, expected_sequence))
        if snapshot.text is None:
            return False, "no previous text"
        if expected_sequence is not None and self.sequence != expected_sequence:
            return False, "sequence changed"
        self.text = snapshot.text
        self.sequence += 1
        return True, "restored"


class InputWorkerTests(unittest.TestCase):
    def setUp(self):
        self.windows_patch = patch("auto_typer.IS_WINDOWS", True)
        self.f8_patch = patch("auto_typer.is_f8_pressed", return_value=False)
        self.guard_patch = patch(
            "auto_typer._send_input",
            side_effect=AssertionError("单元测试不得发送真实键盘事件"),
        )
        self.windows_patch.start()
        self.f8_patch.start()
        self.guard_patch.start()

    def tearDown(self):
        self.guard_patch.stop()
        self.f8_patch.stop()
        self.windows_patch.stop()

    def run_worker(
        self,
        text,
        target,
        interval=0,
        stop_event=None,
        literal_mode=None,
        strategy=auto_typer.INPUT_STRATEGY_AUTO,
        clipboard=None,
        paste_action=None,
        paste_settle=0,
        verification_timeout=0,
        target_factory=None,
        space_mode=auto_typer.SPACE_MODE_VIRTUAL,
        tab_mode=auto_typer.TAB_MODE_VIRTUAL,
        newline_mode=auto_typer.NEWLINE_MODE_ENTER,
    ):
        clipboard = clipboard or FakeClipboard()
        normalized = auto_typer.normalize_newlines(text)

        def default_paste():
            if target is not None and target.snapshot.text is not None:
                expected = target.expected_text(normalized)
                if expected is not None:
                    target.text = expected

        events = queue.Queue()
        factory = target_factory or (lambda: target)
        with (
            patch("auto_typer.time.sleep"),
            patch("auto_typer.capture_clipboard_text", side_effect=clipboard.capture),
            patch("auto_typer.write_clipboard_text", side_effect=clipboard.write),
            patch("auto_typer.read_clipboard_text", side_effect=clipboard.read),
            patch("auto_typer.restore_clipboard_text", side_effect=clipboard.restore),
            patch("auto_typer.send_paste_shortcut", side_effect=paste_action or default_paste) as paste,
        ):
            auto_typer.input_worker(
                text,
                0,
                interval,
                stop_event or threading.Event(),
                events,
                literal_mode,
                target_factory=factory,
                strategy=strategy,
                paste_settle_seconds=paste_settle,
                verification_timeout=verification_timeout,
                space_mode=space_mode,
                tab_mode=tab_mode,
                newline_mode=newline_mode,
            )
        received = []
        while not events.empty():
            received.append(events.get_nowait())
        return received, clipboard, paste

    @patch("auto_typer.send_character", side_effect=AssertionError("直写不得发送键盘事件"))
    def test_worker_uses_direct_write_and_readback_without_keyboard(self, send_character):
        target = FakeTarget("prefix", 6, 6, direct=True)
        received, clipboard, paste = self.run_worker(" \t中\r\nB", target)
        final = received[-1]
        self.assertEqual(final["type"], "done")
        self.assertEqual(final["count"], 5)
        self.assertEqual(final["verification"]["status"], "verified")
        self.assertEqual(final["state"]["method"], "uia_value")
        self.assertEqual(final["state"]["sent_spaces"], 0)
        self.assertEqual(target.writes, ["prefix \t中\nB"])
        self.assertTrue(target.closed)
        self.assertEqual(clipboard.writes, [])
        paste.assert_not_called()
        send_character.assert_not_called()

    def test_worker_explicit_keyboard_reads_back_without_inventing_baseline(self):
        target = FakeTarget("prefix", 6, 6, direct=False, dynamic_capture=True)

        def append_character(char, *_args, **_kwargs):
            target.text += char
            return successful_character_result(char)

        with patch("auto_typer.send_character", side_effect=append_character):
            received, clipboard, paste = self.run_worker(
                "A \t中\n",
                target,
                strategy=auto_typer.INPUT_STRATEGY_KEYBOARD,
            )
        final = received[-1]
        self.assertEqual(final["type"], "done")
        self.assertEqual(final["verification"]["status"], "unverified")
        self.assertIn("输入前未读取目标基线", final["verification"]["reason"])
        self.assertTrue(final["verification"]["readable"])
        self.assertFalse(final["verification"]["exact_match"])
        self.assertEqual(final["verification"]["comparison"]["actual_repr"], repr(target.text))
        self.assertEqual(target.capture_calls, 1)
        self.assertEqual(target.text, "prefixA \t中\n")
        self.assertEqual(final["state"]["sent_spaces"], 1)
        self.assertEqual(final["state"]["sent_tabs"], 1)
        self.assertEqual(final["state"]["sent_enters"], 1)
        self.assertEqual(clipboard.writes, [])
        paste.assert_not_called()
        self.assertTrue(final["diagnostics"]["keyboard_input"])
        self.assertTrue(final["diagnostics"]["enter_input"])

    @patch("auto_typer.send_character", side_effect=successful_character_result)
    def test_worker_reports_target_mismatch_instead_of_claiming_success(self, _send_character):
        target = FakeTarget("", 0, 0, direct=False, dynamic_capture=True)
        received, _clipboard, _paste = self.run_worker(
            "    break\n# comment\n",
            target,
            strategy=auto_typer.INPUT_STRATEGY_KEYBOARD,
        )
        final = received[-1]
        self.assertEqual(final["type"], "done")
        self.assertEqual(final["verification"]["status"], "mismatch")
        self.assertFalse(final["verification"]["exact_match"])
        self.assertEqual(final["verification"]["comparison"]["difference_count"], 3)

    def test_worker_reports_direct_write_mismatch(self):
        target = FakeTarget("", 0, 0, direct=True, write_status="mismatch")
        received, _clipboard, _paste = self.run_worker("A\n", target)
        self.assertEqual(received[-1]["type"], "done")
        self.assertEqual(received[-1]["verification"]["status"], "mismatch")

    @patch("auto_typer.send_character", side_effect=AssertionError("失败直写后不得回退逐键"))
    def test_worker_does_not_fall_back_to_keyboard_after_attempted_write(self, send_character):
        target = FakeTarget("", 0, 0, direct=True)
        target.fail_write = True
        received, clipboard, paste = self.run_worker("A", target)
        final = received[-1]
        self.assertEqual(final["type"], "error")
        self.assertIn("目标可能已改变", final["message"])
        self.assertEqual(clipboard.writes, [])
        paste.assert_not_called()
        send_character.assert_not_called()

    @patch("auto_typer.send_character", side_effect=AssertionError("自动模式不得逐键输入"))
    def test_worker_pastes_and_marks_unavailable_target_as_unverified(self, send_character):
        def unavailable_factory():
            raise TargetUnavailable("no UIA provider")

        received, clipboard, paste = self.run_worker(
            "A", None, target_factory=unavailable_factory
        )
        final = received[-1]
        self.assertEqual(final["type"], "done")
        self.assertEqual(final["verification"]["status"], "unverified")
        self.assertIn("无法连接目标控件", final["verification"]["reason"])
        self.assertEqual(final["diagnostics"]["method"], "clipboard")
        self.assertEqual(clipboard.writes, ["A"])
        paste.assert_called_once_with()
        send_character.assert_not_called()

    @patch("auto_typer.send_character", side_effect=successful_character_result)
    def test_worker_normalizes_before_counts_progress_and_logging(self, _send_character):
        target = FakeTarget("", 0, 0, direct=False)
        received, _clipboard, _paste = self.run_worker(
            "A\r\nB",
            target,
            strategy=auto_typer.INPUT_STRATEGY_KEYBOARD,
        )
        final = received[-1]
        self.assertEqual(final["count"], 3)
        self.assertEqual(final["source"]["newlines"], 1)
        self.assertEqual(final["source"]["line_count"], 2)
        final_progress = next(
            event for event in received
            if event["type"] == "progress" and event["count"] == 3
        )
        self.assertEqual(final_progress["state"], final["state"])
        self.assertEqual(final_progress["character"]["character_repr"], "'B'")
        self.assertTrue(any("'\\n'" in line for line in final["log"]))

    @patch("auto_typer.send_character")
    def test_worker_reports_stop_after_first_character(self, send_character):
        target = FakeTarget("", 0, 0, direct=False)
        stop_event = threading.Event()
        send_character.side_effect = lambda _char, *_args, **_kwargs: (
            stop_event.set(), successful_character_result(_char)
        )[1]
        received, _clipboard, _paste = self.run_worker(
            "ABC",
            target,
            stop_event=stop_event,
            strategy=auto_typer.INPUT_STRATEGY_KEYBOARD,
        )
        final = received[-1]
        self.assertEqual(final["type"], "stopped")
        self.assertEqual(final["count"], 1)
        self.assertEqual(final["state"]["position"], 1)
        self.assertEqual(final["verification"]["status"], "unverified")

    @patch("auto_typer.send_character", side_effect=OSError("模拟输入失败"))
    def test_worker_reports_input_errors(self, _send_character):
        target = FakeTarget("", 0, 0, direct=False)
        received, _clipboard, _paste = self.run_worker(
            "A",
            target,
            strategy=auto_typer.INPUT_STRATEGY_KEYBOARD,
        )
        final = received[-1]
        self.assertEqual(final["type"], "error")
        self.assertEqual(final["count"], 0)
        self.assertIn("模拟输入失败", final["message"])

    @patch("auto_typer.send_character", side_effect=AssertionError("自动模式不得逐键输入"))
    def test_worker_auto_uses_one_clipboard_paste_and_full_readback(self, send_character):
        target = FakeTarget("prefix", 6, 6, direct=False)
        received, clipboard, paste = self.run_worker(CODE_SAMPLE, target)
        final = received[-1]
        self.assertEqual(final["type"], "done")
        self.assertEqual(final["count"], len(CODE_SAMPLE))
        self.assertEqual(final["verification"]["status"], "verified")
        self.assertEqual(final["verification"]["method"], "clipboard")
        self.assertEqual(target.text, "prefix" + CODE_SAMPLE)
        self.assertEqual(clipboard.writes, [CODE_SAMPLE])
        self.assertEqual(clipboard.text, "previous clipboard")
        paste.assert_called_once_with()
        self.assertEqual(final["state"]["method"], "clipboard")
        self.assertEqual(final["state"]["sent_enters"], 0)
        diagnostics = final["diagnostics"]
        self.assertEqual(diagnostics["method"], "clipboard")
        self.assertEqual(diagnostics["clipboard_write"], "success")
        self.assertEqual(diagnostics["paste_shortcut"], "sent")
        self.assertFalse(diagnostics["keyboard_input"])
        self.assertFalse(diagnostics["enter_input"])
        for line in (
            "输入方式：Clipboard Paste",
            "目标控件是否成功获取：是",
            "目标文本是否可读取：是",
            "目标控件是否支持整值写入：否",
            "剪贴板是否成功写入：是",
            "是否执行 Ctrl+V：是",
            "是否执行逐字符输入：否",
            "是否执行 Enter：否",
            f"原始空格数量：{CODE_SAMPLE.count(' ')}",
            f"原始 Tab 数量：{CODE_SAMPLE.count(chr(9))}",
            f"原始换行数量：{CODE_SAMPLE.count(chr(10))}",
            "最终验证状态：verified",
        ):
            self.assertIn(line, final["log"][0])
        send_character.assert_not_called()

    @patch("auto_typer.send_character", side_effect=AssertionError("剪贴板失败不得逐键回退"))
    def test_worker_clipboard_readback_failure_stops_before_paste(self, send_character):
        target = FakeTarget("", 0, 0, direct=False)
        clipboard = FakeClipboard()
        clipboard.read_override = "damaged clipboard"
        received, clipboard, paste = self.run_worker("A\nB", target, clipboard=clipboard)
        final = received[-1]
        self.assertEqual(final["type"], "error")
        self.assertIn("剪贴板写入后读回", final["message"])
        self.assertEqual(final["diagnostics"]["clipboard_write"], "failed")
        self.assertEqual(final["diagnostics"]["paste_shortcut"], "not_used")
        self.assertEqual(clipboard.text, "previous clipboard")
        paste.assert_not_called()
        send_character.assert_not_called()

    @patch("auto_typer.send_character", side_effect=AssertionError("粘贴失败不得逐键回退"))
    def test_worker_paste_failure_is_reported_without_keyboard_fallback(self, send_character):
        target = FakeTarget("", 0, 0, direct=False)
        clipboard = FakeClipboard()

        def fail_paste():
            raise OSError("fake Ctrl+V failure")

        received, clipboard, paste = self.run_worker(
            "A\nB", target, clipboard=clipboard, paste_action=fail_paste
        )
        final = received[-1]
        self.assertEqual(final["type"], "error")
        self.assertIn("fake Ctrl+V failure", final["message"])
        self.assertIn("目标可能已改变", final["message"])
        self.assertEqual(final["diagnostics"]["paste_shortcut"], "failed")
        self.assertEqual(clipboard.text, "previous clipboard")
        paste.assert_called_once_with()
        send_character.assert_not_called()

    @patch("auto_typer.send_character", side_effect=AssertionError("自动模式不得逐键输入"))
    def test_worker_clipboard_mismatch_is_not_reported_as_success(self, send_character):
        target = FakeTarget("", 0, 0, direct=False)
        received, _clipboard, _paste = self.run_worker(
            "    break\n# comment\n", target, paste_action=lambda: None
        )
        final = received[-1]
        self.assertEqual(final["type"], "done")
        self.assertEqual(final["verification"]["status"], "mismatch")
        self.assertEqual(final["verification"]["method"], "clipboard")
        self.assertGreater(final["verification"]["comparison"]["difference_count"], 0)
        send_character.assert_not_called()

    @patch("auto_typer.send_character", side_effect=AssertionError("自动模式不得逐键输入"))
    def test_worker_unreadable_target_uses_paste_but_remains_unverified(self, send_character):
        target = FakeTarget("", direct=False, readable=False)
        received, clipboard, paste = self.run_worker("中文😀\n", target)
        final = received[-1]
        self.assertEqual(final["type"], "done")
        self.assertEqual(final["verification"]["status"], "unverified")
        self.assertEqual(final["verification"]["method"], "clipboard")
        self.assertFalse(final["diagnostics"]["target_readable"])
        self.assertEqual(clipboard.writes, ["中文😀\n"])
        paste.assert_called_once_with()
        send_character.assert_not_called()

    @patch("auto_typer.send_character", side_effect=AssertionError("停止前不得逐键输入"))
    def test_worker_stop_after_clipboard_write_prevents_ctrl_v(self, send_character):
        target = FakeTarget("", 0, 0, direct=False)
        stop_event = threading.Event()
        clipboard = FakeClipboard()
        clipboard.on_write = stop_event.set
        received, clipboard, paste = self.run_worker(
            "A\nB", target, stop_event=stop_event, clipboard=clipboard
        )
        final = received[-1]
        self.assertEqual(final["type"], "stopped")
        self.assertEqual(final["count"], 0)
        self.assertEqual(final["diagnostics"]["clipboard_write"], "success")
        self.assertEqual(final["diagnostics"]["paste_shortcut"], "not_used")
        self.assertEqual(clipboard.text, "previous clipboard")
        paste.assert_not_called()
        send_character.assert_not_called()

    @patch("auto_typer.send_character", side_effect=AssertionError("粘贴后停止不得逐键输入"))
    def test_worker_stop_after_paste_reports_unverified_full_block(self, send_character):
        target = FakeTarget("", 0, 0, direct=False)
        stop_event = threading.Event()

        def paste_then_stop():
            target.text = target.expected_text("A\nB")
            stop_event.set()

        received, _clipboard, paste = self.run_worker(
            "A\nB",
            target,
            stop_event=stop_event,
            paste_action=paste_then_stop,
        )
        final = received[-1]
        self.assertEqual(final["type"], "stopped")
        self.assertEqual(final["count"], 3)
        self.assertEqual(final["verification"]["status"], "unverified")
        self.assertEqual(final["diagnostics"]["paste_shortcut"], "sent")
        self.assertIn("Ctrl+V 已发送", final["verification"]["reason"])
        paste.assert_called_once_with()
        send_character.assert_not_called()

    def test_explicit_keyboard_strategy_bypasses_safe_direct_write_and_clipboard(self):
        target = FakeTarget("", 0, 0, direct=True, dynamic_capture=True)

        def append_character(char, *_args, **_kwargs):
            target.text += char
            return successful_character_result(char)

        with patch("auto_typer.send_character", side_effect=append_character):
            received, clipboard, paste = self.run_worker(
                "A\nB",
                target,
                strategy=auto_typer.INPUT_STRATEGY_KEYBOARD,
            )
        final = received[-1]
        self.assertEqual(final["verification"]["status"], "unverified")
        self.assertEqual(final["state"]["method"], "keyboard")
        self.assertEqual(target.writes, [])
        self.assertEqual(clipboard.writes, [])
        paste.assert_not_called()

    def test_worker_defaults_to_independent_keyboard_without_pre_capture(self):
        events = queue.Queue()
        factory = Mock(side_effect=target_text.TargetUnavailable("no provider"))
        with (
            patch("auto_typer.send_character", side_effect=successful_character_result),
            patch("auto_typer.capture_clipboard_text", side_effect=AssertionError("不得读取剪贴板")),
            patch("auto_typer.write_clipboard_text", side_effect=AssertionError("不得写剪贴板")),
            patch("auto_typer.send_paste_shortcut", side_effect=AssertionError("不得粘贴")),
            patch("auto_typer.get_foreground_window_info", return_value=None),
        ):
            auto_typer.input_worker("A", 0, 0, threading.Event(), events, target_factory=factory)
        received = []
        while not events.empty():
            received.append(events.get_nowait())
        final = received[-1]
        self.assertEqual(final["type"], "done")
        self.assertEqual(final["diagnostics"]["requested_strategy"], auto_typer.INPUT_STRATEGY_KEYBOARD)
        self.assertEqual(final["diagnostics"]["method"], "keyboard")
        self.assertEqual(final["diagnostics"]["target_capture_phase"], "after_input")
        self.assertEqual(final["diagnostics"]["clipboard_write"], "not_used")
        self.assertEqual(final["diagnostics"]["paste_shortcut"], "not_used")
        self.assertEqual(final["verification"]["status"], "unverified")
        self.assertEqual([event["type"] for event in received[:2]], ["typing", "diagnostic"])
        self.assertEqual(received[-2]["phase"], "after_input")
        factory.assert_called_once_with()

    def test_worker_keyboard_partial_failure_never_falls_back(self):
        target = FakeTarget("", 0, 0, direct=True, dynamic_capture=True)
        partial = auto_typer.EventBatchResult(2, 1, True, False, True, "partial")

        def send(char):
            if char == "B":
                raise auto_typer.SendInputError("partial", partial, 5)
            return successful_character_result(char)

        with patch("auto_typer.send_character", side_effect=send):
            received, clipboard, paste = self.run_worker(
                "ABC", target, strategy=auto_typer.INPUT_STRATEGY_KEYBOARD
            )
        final = received[-1]
        self.assertEqual(final["type"], "error")
        self.assertEqual(final["count"], 1)
        self.assertEqual(final["diagnostics"]["failed_character_position"], 2)
        self.assertEqual(final["diagnostics"]["sendinput_requested"], 4)
        self.assertEqual(final["diagnostics"]["sendinput_accepted"], 3)
        self.assertEqual(final["character_events"][-1]["status"], "failed")
        self.assertEqual(final["verification"]["status"], "unverified")
        self.assertEqual(target.capture_calls, 0)
        self.assertEqual(target.writes, [])
        self.assertEqual(clipboard.writes, [])
        paste.assert_not_called()

    def test_worker_forwards_independent_modes_and_logs_each_character(self):
        def unavailable_factory():
            raise target_text.TargetUnavailable("no provider")

        def send(char, *_args, **kwargs):
            options = kwargs.get("options") or auto_typer.CharacterOptions()
            return successful_character_result(
                char, auto_typer._dispatch_method(char, options)
            )

        with patch("auto_typer.send_character", side_effect=send) as send_character:
            received, clipboard, paste = self.run_worker(
                " \t\n",
                None,
                strategy=auto_typer.INPUT_STRATEGY_KEYBOARD,
                target_factory=unavailable_factory,
                space_mode=auto_typer.SPACE_MODE_VIRTUAL,
                tab_mode=auto_typer.TAB_MODE_UNICODE,
                newline_mode=auto_typer.NEWLINE_MODE_UNICODE_LF,
            )
        final = received[-1]
        options = auto_typer.CharacterOptions(
            auto_typer.SPACE_MODE_VIRTUAL,
            auto_typer.TAB_MODE_UNICODE,
            auto_typer.NEWLINE_MODE_UNICODE_LF,
        )
        self.assertEqual(send_character.call_args_list, [call(char, options=options) for char in " \t\n"])
        self.assertEqual([event["method"] for event in final["character_events"]], ["vk_space", "unicode_tab", "unicode_lf"])
        self.assertEqual(final["diagnostics"]["space_mode"], "virtual")
        self.assertEqual(final["diagnostics"]["tab_mode"], "unicode")
        self.assertEqual(final["diagnostics"]["newline_mode"], "unicode_lf")
        self.assertEqual(final["source"]["spaces"], 1)
        self.assertEqual(final["source"]["tabs"], 1)
        self.assertEqual(final["source"]["newlines"], 1)
        self.assertEqual(clipboard.writes, [])
        paste.assert_not_called()

    def test_worker_stop_after_last_character_skips_readback(self):
        target = FakeTarget("", 0, 0, dynamic_capture=True)
        stop_event = threading.Event()

        def send(char):
            stop_event.set()
            return successful_character_result(char)

        with patch("auto_typer.send_character", side_effect=send):
            received, _clipboard, _paste = self.run_worker(
                "A",
                target,
                stop_event=stop_event,
                strategy=auto_typer.INPUT_STRATEGY_KEYBOARD,
            )
        final = received[-1]
        self.assertEqual(final["type"], "done")
        self.assertEqual(final["count"], 1)
        self.assertEqual(final["verification"]["status"], "unverified")
        self.assertIn("结果比较前已停止", final["verification"]["reason"])
        self.assertEqual(target.capture_calls, 0)

    def test_worker_keyboard_unreadable_target_remains_unverified(self):
        target = FakeTarget("", 0, 0, readable=False, dynamic_capture=True)
        with patch("auto_typer.send_character", side_effect=successful_character_result):
            received, clipboard, paste = self.run_worker(
                "A", target, strategy=auto_typer.INPUT_STRATEGY_KEYBOARD
            )
        final = received[-1]
        self.assertEqual(final["type"], "done")
        self.assertEqual(final["verification"]["status"], "unverified")
        self.assertTrue(final["diagnostics"]["target_captured"])
        self.assertFalse(final["diagnostics"]["target_readable"])
        self.assertEqual(clipboard.writes, [])
        paste.assert_not_called()

    def test_worker_records_foreground_focus_before_and_after_keyboard_input(self):
        before = {"foreground_window": 1, "process_id": 10, "focus_window": 11}
        after = {"foreground_window": 2, "process_id": 20, "focus_window": 22}
        with (
            patch("auto_typer.send_character", side_effect=successful_character_result),
            patch("auto_typer.get_foreground_window_info", side_effect=[before, after]),
        ):
            received, _clipboard, _paste = self.run_worker(
                "A", None, strategy=auto_typer.INPUT_STRATEGY_KEYBOARD
            )
        diagnostics = received[-1]["diagnostics"]
        self.assertEqual(diagnostics["foreground_before"], before)
        self.assertEqual(diagnostics["foreground_after"], after)
        self.assertFalse(diagnostics["focus_preserved"])

    def test_worker_bounds_character_event_buffer_without_losing_total_count(self):
        with patch("auto_typer.send_character", side_effect=successful_character_result):
            received, _clipboard, _paste = self.run_worker(
                "A" * 5001, None, strategy=auto_typer.INPUT_STRATEGY_KEYBOARD
            )
        final = received[-1]
        self.assertEqual(final["character_event_count"], 5001)
        self.assertEqual(len(final["character_events"]), auto_typer.MAX_CHARACTER_EVENTS)
        self.assertTrue(final["character_events_truncated"])
        self.assertEqual(final["character_events"][0]["position"], 2)
        self.assertEqual(final["character_events"][-1]["position"], 5001)

    def test_worker_honors_stop_event_during_countdown(self):
        events = queue.Queue()
        stop_event = threading.Event()
        stop_event.set()
        auto_typer.input_worker("A", 5, 0, stop_event, events)
        self.assertEqual(events.get_nowait()["type"], "stopped")

    @patch("auto_typer.is_f8_pressed", return_value=True)
    def test_worker_honors_f8_during_countdown(self, _is_f8):
        events = queue.Queue()
        auto_typer.input_worker("A", 5, 0, threading.Event(), events)
        self.assertEqual(events.get_nowait()["type"], "stopped")

    @patch("auto_typer.IS_WINDOWS", False)
    def test_worker_reports_non_windows_without_calling_api(self):
        events = queue.Queue()
        auto_typer.input_worker("A", 0, 0, threading.Event(), events)
        event = events.get_nowait()
        self.assertEqual(event["type"], "error")
        self.assertEqual(event["count"], 0)
        self.assertIn("仅支持 Windows", event["message"])

    @patch("auto_typer.send_character")
    def test_worker_rejects_nul_before_input(self, send_character):
        target = FakeTarget("", 0, 0, direct=False)
        received, clipboard, paste = self.run_worker("A\x00B", target)
        self.assertEqual(received[-1]["type"], "error")
        self.assertIn("NUL", received[-1]["message"])
        self.assertEqual(clipboard.writes, [])
        paste.assert_not_called()
        send_character.assert_not_called()


@unittest.skipUnless(clipboard_text.IS_WINDOWS, "Windows 剪贴板 API 仅在 Windows 上验证")
class ClipboardApiTests(unittest.TestCase):
    def test_read_clipboard_text_preserves_complete_unicode_block(self):
        payload = CODE_SAMPLE.encode("utf-16-le") + b"\x00\x00"
        handle = clipboard_text.kernel32.GlobalAlloc(
            clipboard_text.GMEM_MOVEABLE, len(payload)
        )
        self.assertTrue(handle)
        locked = clipboard_text.kernel32.GlobalLock(handle)
        self.assertTrue(locked)
        try:
            ctypes.memmove(locked, payload, len(payload))
        finally:
            clipboard_text.kernel32.GlobalUnlock(handle)
        fake_user32 = Mock()
        fake_user32.IsClipboardFormatAvailable.return_value = True
        fake_user32.GetClipboardData.return_value = handle
        try:
            with (
                patch.object(clipboard_text, "user32", fake_user32),
                patch.object(
                    clipboard_text, "_clipboard_session", return_value=nullcontext()
                ),
            ):
                text = clipboard_text.read_clipboard_text()
            self.assertEqual(text, CODE_SAMPLE)
            fake_user32.IsClipboardFormatAvailable.assert_called_once_with(
                clipboard_text.CF_UNICODETEXT
            )
            fake_user32.GetClipboardData.assert_called_once_with(
                clipboard_text.CF_UNICODETEXT
            )
        finally:
            clipboard_text.kernel32.GlobalFree(handle)

    def test_write_clipboard_text_stores_exact_block_and_returns_sequence(self):
        captured = {}
        fake_user32 = Mock()
        fake_user32.EmptyClipboard.return_value = True
        fake_user32.GetClipboardSequenceNumber.return_value = 42

        def capture_data(format_id, handle):
            captured["format"] = format_id
            locked = clipboard_text.kernel32.GlobalLock(handle)
            size = clipboard_text.kernel32.GlobalSize(handle)
            try:
                captured["text"] = ctypes.wstring_at(locked, size // 2 - 1)
            finally:
                clipboard_text.kernel32.GlobalUnlock(handle)
            return handle

        fake_user32.SetClipboardData.side_effect = capture_data
        with (
            patch.object(clipboard_text, "user32", fake_user32),
            patch.object(
                clipboard_text, "_clipboard_session", return_value=nullcontext()
            ),
        ):
            sequence = clipboard_text.write_clipboard_text(CODE_SAMPLE)
        try:
            self.assertEqual(sequence, 42)
            self.assertEqual(captured["format"], clipboard_text.CF_UNICODETEXT)
            self.assertEqual(captured["text"], CODE_SAMPLE)
            fake_user32.EmptyClipboard.assert_called_once_with()
        finally:
            handle = fake_user32.SetClipboardData.call_args.args[1]
            clipboard_text.kernel32.GlobalFree(handle)

    def test_missing_unicode_clipboard_format_returns_none(self):
        fake_user32 = Mock()
        fake_user32.IsClipboardFormatAvailable.return_value = False
        with (
            patch.object(clipboard_text, "user32", fake_user32),
            patch.object(
                clipboard_text, "_clipboard_session", return_value=nullcontext()
            ),
        ):
            self.assertIsNone(clipboard_text.read_clipboard_text())
        fake_user32.GetClipboardData.assert_not_called()

    def test_restore_requires_matching_sequence_and_preserves_original_text(self):
        snapshot = ClipboardSnapshot("original", 1)
        with (
            patch.object(clipboard_text, "_clipboard_sequence_number", return_value=2),
            patch.object(clipboard_text, "write_clipboard_text") as write,
        ):
            restored, reason = clipboard_text.restore_clipboard_text(snapshot, 2)
        self.assertTrue(restored)
        self.assertIn("已恢复", reason)
        write.assert_called_once_with("original")

        with patch.object(clipboard_text, "_clipboard_sequence_number", return_value=3):
            restored, reason = clipboard_text.restore_clipboard_text(snapshot, 2)
        self.assertFalse(restored)
        self.assertIn("序列已变化", reason)


class ClipboardPlatformTests(unittest.TestCase):
    @patch.object(clipboard_text, "IS_WINDOWS", False)
    def test_non_windows_clipboard_calls_raise_clear_error(self):
        with self.assertRaisesRegex(clipboard_text.ClipboardError, "不支持 Windows"):
            clipboard_text.read_clipboard_text()
        with self.assertRaisesRegex(clipboard_text.ClipboardError, "不支持 Windows"):
            clipboard_text.write_clipboard_text("A")


@unittest.skipUnless(auto_typer.IS_WINDOWS, "Windows 结构仅在 Windows 上验证")
class WindowsInputStructureTests(unittest.TestCase):
    @patch("auto_typer._send_input")
    def test_unicode_emoji_uses_surrogate_pairs_and_unicode_flags(self, send_input):
        auto_typer.send_unicode_character("😀")
        events = [event for invocation in send_input.call_args_list for event in invocation.args[0]]
        self.assertEqual([event.data.ki.wVk for event in events], [0, 0, 0, 0])
        self.assertEqual([event.data.ki.wScan for event in events], [0xD83D, 0xD83D, 0xDE00, 0xDE00])
        self.assertEqual(
            [event.data.ki.dwFlags for event in events],
            [
                auto_typer.KEYEVENTF_UNICODE,
                auto_typer.KEYEVENTF_UNICODE | auto_typer.KEYEVENTF_KEYUP,
                auto_typer.KEYEVENTF_UNICODE,
                auto_typer.KEYEVENTF_UNICODE | auto_typer.KEYEVENTF_KEYUP,
            ],
        )

    @patch("auto_typer._send_input")
    def test_virtual_key_is_always_released(self, send_input):
        auto_typer.send_key(auto_typer.VK_TAB)
        events = send_input.call_args.args[0]
        self.assertEqual([event.data.ki.wVk for event in events], [auto_typer.VK_TAB] * 2)
        self.assertEqual([event.data.ki.dwFlags for event in events], [0, auto_typer.KEYEVENTF_KEYUP])

    @patch("auto_typer.ctypes.get_last_error", return_value=5)
    @patch("auto_typer.user32")
    def test_partial_send_input_raises_windows_error(self, user32, _get_last_error):
        user32.SendInput.return_value = 0
        event = auto_typer._keyboard_event(auto_typer.VK_RETURN, 0, 0)
        with self.assertRaises(OSError) as raised:
            auto_typer._send_input((event,))
        self.assertEqual(raised.exception.args[0], 5)
        self.assertTrue(str(raised.exception))

    @patch("auto_typer.ctypes.get_last_error", return_value=5)
    @patch("auto_typer._raw_send_input")
    def test_partial_virtual_key_releases_accepted_key_down(self, raw_send, _get_last_error):
        raw_send.side_effect = [1, 1]
        with self.assertRaises(auto_typer.SendInputError) as raised:
            auto_typer.send_key(auto_typer.VK_TAB)
        result = raised.exception.result
        self.assertEqual((result.requested, result.accepted), (2, 1))
        self.assertTrue(result.key_down)
        self.assertFalse(result.key_up)
        self.assertTrue(result.recovered)
        cleanup = raw_send.call_args_list[1].args[0]
        self.assertEqual(len(cleanup), 1)
        self.assertEqual(cleanup[0].data.ki.wVk, auto_typer.VK_TAB)
        self.assertEqual(cleanup[0].data.ki.dwFlags, auto_typer.KEYEVENTF_KEYUP)

    @patch("auto_typer.ctypes.get_last_error", return_value=5)
    @patch("auto_typer._raw_send_input")
    def test_failed_key_up_cleanup_is_reported(self, raw_send, _get_last_error):
        raw_send.side_effect = [1, 0]
        with self.assertRaises(auto_typer.SendInputError) as raised:
            auto_typer.send_key(auto_typer.VK_RETURN)
        self.assertFalse(raised.exception.result.recovered)
        self.assertIn("清理失败", str(raised.exception))

    @patch("auto_typer.ctypes.get_last_error", side_effect=[5, 6])
    @patch("auto_typer._raw_send_input")
    def test_cleanup_exception_does_not_replace_original_windows_error(self, raw_send, get_last_error):
        raw_send.side_effect = [1, OSError("cleanup failed")]
        with self.assertRaises(auto_typer.SendInputError) as raised:
            auto_typer.send_key(auto_typer.VK_SPACE)
        self.assertEqual(raised.exception.args[0], 5)
        self.assertFalse(raised.exception.result.recovered)
        self.assertIn("cleanup failed", raised.exception.result.error)
        get_last_error.assert_called_once_with()

    @patch("auto_typer.ctypes.get_last_error", return_value=5)
    @patch("auto_typer._raw_send_input")
    def test_partial_surrogate_pair_releases_only_accepted_low_unit(self, raw_send, _get_last_error):
        raw_send.side_effect = [3, 1]
        with self.assertRaises(auto_typer.SendInputError) as raised:
            auto_typer.send_unicode_character("😀")
        result = raised.exception.result
        self.assertEqual((result.requested, result.accepted), (4, 3))
        self.assertTrue(result.key_down)
        self.assertFalse(result.key_up)
        self.assertTrue(result.recovered)
        cleanup = raw_send.call_args_list[1].args[0]
        self.assertEqual(len(cleanup), 1)
        self.assertEqual(cleanup[0].data.ki.wScan, 0xDE00)
        self.assertEqual(
            cleanup[0].data.ki.dwFlags,
            auto_typer.KEYEVENTF_UNICODE | auto_typer.KEYEVENTF_KEYUP,
        )

    @patch("auto_typer._raw_send_input", return_value=2)
    def test_complete_virtual_key_returns_structured_success(self, raw_send):
        result = auto_typer.send_key(auto_typer.VK_TAB)
        self.assertEqual((result.requested, result.accepted), (2, 2))
        self.assertTrue(result.key_down)
        self.assertTrue(result.key_up)
        self.assertTrue(result.recovered)
        raw_send.assert_called_once()

    def test_pressed_keys_are_released_in_reverse_order(self):
        events = (
            auto_typer._keyboard_event(auto_typer.VK_CONTROL, 0, 0),
            auto_typer._keyboard_event(auto_typer.VK_V, 0, 0),
        )
        self.assertEqual(
            auto_typer._pressed_keys(events),
            [(auto_typer.VK_V, 0, 0), (auto_typer.VK_CONTROL, 0, 0)],
        )

    def test_uia_variant_layout_and_guids_match_windows_abi(self):
        self.assertEqual(ctypes.sizeof(target_text.VARIANT), 24 if ctypes.sizeof(ctypes.c_void_p) == 8 else 16)
        self.assertEqual(target_text._guid(target_text.IID_IUIAutomationValuePattern).Data1, 0xA94CD8B1)
        self.assertEqual(target_text.UIA_ValuePatternId, 10002)
        self.assertEqual(target_text.UIA_TextPatternId, 10014)

    def test_uninitialized_target_controller_can_close_safely(self):
        controller = target_text.TargetController()
        controller.close()
        self.assertEqual(controller.snapshot, None)

    @unittest.skipUnless(auto_typer.IS_WINDOWS, "Windows API")
    @patch("auto_typer.IS_WINDOWS", True)
    @patch("auto_typer.user32")
    def test_foreground_info_allows_missing_focus_window(self, user32):
        user32.GetForegroundWindow.return_value = 100

        def get_process_id(_hwnd, process_id):
            process_id._obj.value = 10
            return 20

        def get_gui_info(_thread_id, info):
            info._obj.hwndFocus = None
            return 1

        user32.GetWindowThreadProcessId.side_effect = get_process_id
        user32.GetGUIThreadInfo.side_effect = get_gui_info
        self.assertEqual(
            auto_typer.get_foreground_window_info(),
            {"foreground_window": 100, "process_id": 10, "focus_window": None},
        )


@unittest.skipIf(auto_typer.tk is None, "Tkinter 不可用")
class HiddenUiTests(unittest.TestCase):
    def test_hidden_ui_preserves_final_newline_and_shows_parser_details(self):
        root = auto_typer.tk.Tk()
        root.withdraw()
        try:
            app = auto_typer.AutoTyperApp(root)
            self.assertEqual(root.title(), "自动键盘录入助手 V2.3")
            self.assertEqual(str(app.literal_check.cget("state")), "normal")
            self.assertEqual(str(app.paste_settle_spin.cget("state")), "disabled")
            self.assertEqual(str(app.space_combo.get()), next(iter(auto_typer.SPACE_MODES)))
            self.assertEqual(str(app.tab_combo.get()), next(iter(auto_typer.TAB_MODES)))
            self.assertEqual(str(app.newline_combo.get()), next(iter(auto_typer.NEWLINE_MODES)))
            auto_label = next(
                label
                for label, strategy in auto_typer.INPUT_STRATEGIES.items()
                if strategy == auto_typer.INPUT_STRATEGY_AUTO
            )
            app.strategy_name.set(auto_label)
            app.apply_strategy()
            self.assertEqual(str(app.literal_check.cget("state")), "disabled")
            self.assertEqual(str(app.paste_settle_spin.cget("state")), "normal")
            app.editor.delete("1.0", "end")
            app.editor.insert("1.0", " \tA  \n")
            self.assertEqual(app.current_text(), " \tA  \n")
            app.update_content_analysis()
            self.assertIn("末尾换行 是", app.stats_text.get())
            self.assertIn("' \\tA  '", app.line_details.get("1.0", "end-1c"))
            root.update_idletasks()
        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main()
