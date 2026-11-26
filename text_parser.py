from __future__ import annotations

import hashlib
from dataclasses import dataclass


def normalize_newlines(text: str) -> str:
    """只统一换行格式，不裁剪空格、Tab 或正文。"""
    return text.replace("\r\n", "\n").replace("\r", "\n")


@dataclass(frozen=True)
class LineInfo:
    number: int
    content: str
    terminator: str
    leading_spaces: int
    leading_tabs: int
    leading_whitespace: str
    trailing_spaces: int
    inner_tabs: int
    is_blank: bool
    is_last_line: bool

    @property
    def leading_whitespace_count(self) -> int:
        return self.leading_spaces + self.leading_tabs

    def as_dict(self) -> dict:
        return {
            "number": self.number,
            "content": self.content,
            "content_repr": repr(self.content),
            "terminator": self.terminator,
            "leading_spaces": self.leading_spaces,
            "leading_tabs": self.leading_tabs,
            "leading_whitespace_count": self.leading_whitespace_count,
            "leading_whitespace_repr": repr(self.leading_whitespace),
            "trailing_spaces": self.trailing_spaces,
            "inner_tabs": self.inner_tabs,
            "is_blank": self.is_blank,
            "is_last_line": self.is_last_line,
        }


@dataclass(frozen=True)
class ParsedText:
    original_text: str
    normalized_text: str
    lines: tuple[LineInfo, ...]
    ends_with_newline: bool
    sha256: str

    @property
    def line_count(self) -> int:
        return len(self.lines)

    @property
    def newline_count(self) -> int:
        return self.normalized_text.count("\n")

    @property
    def spaces(self) -> int:
        return self.normalized_text.count(" ")

    @property
    def tabs(self) -> int:
        return self.normalized_text.count("\t")

    @property
    def blank_line_count(self) -> int:
        return sum(line.is_blank for line in self.lines)

    @property
    def max_consecutive_blank_lines(self) -> int:
        maximum = 0
        current = 0
        for line in self.lines:
            current = current + 1 if line.is_blank else 0
            maximum = max(maximum, current)
        return maximum

    def reconstruct(self) -> str:
        return "".join(line.content + line.terminator for line in self.lines)

    def character_line_numbers(self) -> tuple[int, ...]:
        numbers = []
        for line in self.lines:
            numbers.extend([line.number] * len(line.content))
            if line.terminator:
                numbers.append(line.number)
        return tuple(numbers)

    def as_dict(self) -> dict:
        return {
            "original_text": self.original_text,
            "original_text_repr": repr(self.original_text),
            "normalized_text": self.normalized_text,
            "normalized_text_repr": repr(self.normalized_text),
            "sha256": self.sha256,
            "line_count": self.line_count,
            "newline_count": self.newline_count,
            "spaces": self.spaces,
            "tabs": self.tabs,
            "blank_line_count": self.blank_line_count,
            "max_consecutive_blank_lines": self.max_consecutive_blank_lines,
            "ends_with_newline": self.ends_with_newline,
            "lines": [line.as_dict() for line in self.lines],
        }


def _parse_line(
    content: str,
    number: int,
    terminator: str,
    is_last: bool,
) -> LineInfo:
    leading_end = 0
    while leading_end < len(content) and content[leading_end] in (" ", "\t"):
        leading_end += 1
    leading = content[:leading_end]

    trailing_spaces = 0
    while (
        trailing_spaces < len(content)
        and content[len(content) - trailing_spaces - 1] == " "
    ):
        trailing_spaces += 1

    leading_tabs = leading.count("\t")
    return LineInfo(
        number=number,
        content=content,
        terminator=terminator,
        leading_spaces=leading.count(" "),
        leading_tabs=leading_tabs,
        leading_whitespace=leading,
        trailing_spaces=trailing_spaces,
        inner_tabs=content.count("\t") - leading_tabs,
        is_blank=all(char in (" ", "\t") for char in content),
        is_last_line=is_last,
    )


def parse_text(text: str) -> ParsedText:
    """逐字符解析原文；返回对象可无损重建规范化文本。"""
    if not isinstance(text, str):
        raise TypeError("text 必须是字符串")

    normalized = normalize_newlines(text)
    lines: list[LineInfo] = []
    current: list[str] = []

    for char in normalized:
        if char == "\n":
            lines.append(_parse_line("".join(current), len(lines) + 1, "\n", False))
            current = []
        else:
            current.append(char)

    if normalized:
        lines.append(_parse_line("".join(current), len(lines) + 1, "", True))

    frozen_lines = tuple(lines)
    return ParsedText(
        original_text=text,
        normalized_text=normalized,
        lines=frozen_lines,
        ends_with_newline=normalized.endswith("\n"),
        sha256=hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
    )


def format_line_analysis(parsed: ParsedText) -> str:
    """显示每行真实空白序列和 repr 内容，不按显示宽度推算字符数。"""
    if not parsed.lines:
        return "（空内容，没有可分析的行）"

    rows = [
        f"原文 SHA-256: {parsed.sha256}",
        f"逻辑行数 {parsed.line_count}；空行 {parsed.blank_line_count}；"
        f"末尾换行 {'是' if parsed.ends_with_newline else '否'}",
    ]
    for line in parsed.lines:
        rows.append(
            f"第{line.number}行  行首空格 {line.leading_spaces}  "
            f"行首Tab {line.leading_tabs}  行首空白 {line.leading_whitespace_count}  "
            f"行尾空格 {line.trailing_spaces}  行内Tab {line.inner_tabs}  "
            f"空行 {'是' if line.is_blank else '否'}  "
            f"末行 {'是' if line.is_last_line else '否'}  "
            f"行首序列 {line.leading_whitespace!r}  原文 {line.content!r}"
        )
    return "\n".join(rows)


def insert_text(text: str, insertion: str, start: int, end: int | None = None) -> str:
    """在已确认的文本偏移处插入快照，除此之外不改写目标文本。"""
    if end is None:
        end = start
    if not 0 <= start <= end <= len(text):
        raise ValueError("插入偏移超出目标文本范围")
    return text[:start] + insertion + text[end:]


def compare_text(expected: str, actual: str) -> dict:
    """比较逻辑文本及逐行格式；换行表示格式差异不被视为内容差异。"""
    expected_normalized = normalize_newlines(expected)
    actual_normalized = normalize_newlines(actual)
    expected_parsed = parse_text(expected_normalized)
    actual_parsed = parse_text(actual_normalized)

    differences = []
    for index in range(max(expected_parsed.line_count, actual_parsed.line_count)):
        expected_line = (
            expected_parsed.lines[index]
            if index < expected_parsed.line_count
            else None
        )
        actual_line = actual_parsed.lines[index] if index < actual_parsed.line_count else None
        expected_info = expected_line.as_dict() if expected_line else None
        actual_info = actual_line.as_dict() if actual_line else None
        if expected_info != actual_info:
            differences.append(
                {
                    "line_number": index + 1,
                    "expected": expected_info,
                    "actual": actual_info,
                }
            )

    exact_match = expected_normalized == actual_normalized
    return {
        "status": "verified" if exact_match else "mismatch",
        "exact_match": exact_match,
        "expected_total": len(expected_normalized),
        "actual_total": len(actual_normalized),
        "expected_lines": expected_parsed.line_count,
        "actual_lines": actual_parsed.line_count,
        "expected_newlines": expected_parsed.newline_count,
        "actual_newlines": actual_parsed.newline_count,
        "expected_spaces": expected_parsed.spaces,
        "actual_spaces": actual_parsed.spaces,
        "expected_tabs": expected_parsed.tabs,
        "actual_tabs": actual_parsed.tabs,
        "expected_blank_lines": expected_parsed.blank_line_count,
        "actual_blank_lines": actual_parsed.blank_line_count,
        "expected_ends_with_newline": expected_parsed.ends_with_newline,
        "actual_ends_with_newline": actual_parsed.ends_with_newline,
        "differences": differences[:50],
        "difference_count": len(differences),
        "expected_repr": repr(expected_normalized[:240]),
        "actual_repr": repr(actual_normalized[:240]),
    }
