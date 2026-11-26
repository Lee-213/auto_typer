from __future__ import annotations

import ctypes
import math
import queue
import string
import sys
import threading
import time
import unicodedata
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from functools import wraps

from clipboard_text import (
    ClipboardSnapshot,
    capture_clipboard_text,
    read_clipboard_text,
    restore_clipboard_text,
    write_clipboard_text,
)
from target_text import TargetController, TargetSnapshot, TargetUnavailable, Verification
from text_parser import (
    ParsedText,
    compare_text,
    format_line_analysis as format_parsed_line_analysis,
    normalize_newlines,
    parse_text,
)

try:
    import tkinter as tk
    from tkinter import messagebox, ttk
except ImportError:
    tk = None
    messagebox = None
    ttk = None

IS_WINDOWS = sys.platform == "win32"

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
VK_TAB = 0x09
VK_RETURN = 0x0D
VK_SPACE = 0x20
VK_CONTROL = 0x11
VK_V = 0x56
VK_F8 = 0x77

INPUT_STRATEGY_KEYBOARD = "keyboard"
INPUT_STRATEGY_AUTO = "auto"
INPUT_STRATEGIES = {
    "逐字符模拟键盘输入（默认）": INPUT_STRATEGY_KEYBOARD,
    "整段输入（UI Automation → 剪贴板粘贴）": INPUT_STRATEGY_AUTO,
}
SPACE_MODE_UNICODE = "unicode"
SPACE_MODE_VIRTUAL = "virtual"
TAB_MODE_VIRTUAL = "virtual"
TAB_MODE_UNICODE = "unicode"
NEWLINE_MODE_ENTER = "vk_return"
NEWLINE_MODE_UNICODE_LF = "unicode_lf"
SPACE_MODES = {
    "实体 VK_SPACE（记事本实测默认）": SPACE_MODE_VIRTUAL,
    "Unicode U+0020（实验）": SPACE_MODE_UNICODE,
}
TAB_MODES = {
    "实体 VK_TAB（记事本实测默认）": TAB_MODE_VIRTUAL,
    "Unicode U+0009（实验）": TAB_MODE_UNICODE,
}
NEWLINE_MODES = {
    "真实 Enter / VK_RETURN（默认）": NEWLINE_MODE_ENTER,
    "Unicode LF（实验）": NEWLINE_MODE_UNICODE_LF,
}
PASTE_SETTLE_SECONDS = 0.5
PASTE_VERIFY_TIMEOUT_SECONDS = 5.0
MAX_CHARACTER_EVENTS = 5000

WINDOWS_API_ERROR: str | None = None
user32 = None
_INPUT_ENGINE_LOCK = threading.RLock()


def _exclusive_input(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        if not _INPUT_ENGINE_LOCK.acquire(blocking=False):
            raise RuntimeError("已有输入任务正在执行，禁止同时运行多个输入线程")
        try:
            return function(*args, **kwargs)
        finally:
            _INPUT_ENGINE_LOCK.release()

    return wrapped

if IS_WINDOWS:
    from ctypes import wintypes

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", wintypes.LONG),
            ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.c_size_t),
        ]

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.c_size_t),
        ]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [
            ("uMsg", wintypes.DWORD),
            ("wParamL", wintypes.WORD),
            ("wParamH", wintypes.WORD),
        ]

    class GUITHREADINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("flags", wintypes.DWORD),
            ("hwndActive", wintypes.HWND),
            ("hwndFocus", wintypes.HWND),
            ("hwndCapture", wintypes.HWND),
            ("hwndMenuOwner", wintypes.HWND),
            ("hwndMoveSize", wintypes.HWND),
            ("hwndCaret", wintypes.HWND),
            ("rcCaret", wintypes.RECT),
        ]

    class INPUT_UNION(ctypes.Union):
        _fields_ = [
            ("mi", MOUSEINPUT),
            ("ki", KEYBDINPUT),
            ("hi", HARDWAREINPUT),
        ]

    class INPUT(ctypes.Structure):
        _fields_ = [
            ("type", wintypes.DWORD),
            ("data", INPUT_UNION),
        ]

    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.SendInput.argtypes = [
            wintypes.UINT,
            ctypes.POINTER(INPUT),
            ctypes.c_int,
        ]
        user32.SendInput.restype = wintypes.UINT
        user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
        user32.GetAsyncKeyState.restype = ctypes.c_short
        user32.GetForegroundWindow.argtypes = []
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        user32.GetGUIThreadInfo.argtypes = [wintypes.DWORD, ctypes.POINTER(GUITHREADINFO)]
        user32.GetGUIThreadInfo.restype = wintypes.BOOL
    except (OSError, AttributeError) as exc:
        WINDOWS_API_ERROR = f"无法初始化 Windows 键盘输入 API：{exc}"


# 换行后只等待目标程序处理事件，不发送任何缩进调整按键。
NEWLINE_SETTLE_SECONDS = 0.05


@dataclass(frozen=True)
class EventBatchResult:
    requested: int
    accepted: int
    key_down: bool | None = None
    key_up: bool | None = None
    recovered: bool = True
    error: str | None = None

    def as_dict(self) -> dict:
        return {
            "requested": self.requested,
            "accepted": self.accepted,
            "key_down": self.key_down,
            "key_up": self.key_up,
            "recovered": self.recovered,
            "error": self.error,
        }


@dataclass(frozen=True)
class CharacterSendResult:
    character: str
    method: str
    utf16_units: tuple[int, ...]
    batches: tuple[EventBatchResult, ...]

    @property
    def requested(self) -> int:
        return sum(batch.requested for batch in self.batches)

    @property
    def accepted(self) -> int:
        return sum(batch.accepted for batch in self.batches)

    @property
    def key_down_success(self) -> bool:
        return all(batch.key_down is True for batch in self.batches)

    @property
    def key_up_success(self) -> bool:
        return all(batch.key_up is True for batch in self.batches)


@dataclass(frozen=True)
class CharacterOptions:
    space_mode: str = SPACE_MODE_VIRTUAL
    tab_mode: str = TAB_MODE_VIRTUAL
    newline_mode: str = NEWLINE_MODE_ENTER


@dataclass(frozen=True)
class CharacterEventRecord:
    position: int
    character_repr: str
    codepoints: tuple[str, ...]
    utf16_units: tuple[int, ...]
    kind: str
    method: str
    requested: int | None
    accepted: int | None
    key_down_success: bool | None
    key_up_success: bool | None
    status: str
    error: str | None = None

    def as_dict(self) -> dict:
        return {
            "position": self.position,
            "character_repr": self.character_repr,
            "codepoints": list(self.codepoints),
            "utf16_units": [f"0x{unit:04X}" for unit in self.utf16_units],
            "kind": self.kind,
            "is_space": self.kind == "space",
            "is_tab": self.kind == "tab",
            "is_newline": self.kind == "newline",
            "method": self.method,
            "requested": self.requested,
            "accepted": self.accepted,
            "key_down_success": self.key_down_success,
            "key_up_success": self.key_up_success,
            "status": self.status,
            "error": self.error,
        }


class SendInputError(OSError):
    def __init__(
        self,
        message: str,
        result: EventBatchResult,
        error_code: int | None = None,
    ) -> None:
        if error_code is None:
            super().__init__(message)
        else:
            super().__init__(error_code, message)
        self.result = result


def _character_kind(char: str) -> str:
    if char == " ":
        return "space"
    if char == "\t":
        return "tab"
    if char in ("\n", "\r"):
        return "newline"
    return "printable"


def _format_flag(value: bool | None) -> str:
    return "未知" if value is None else ("成功" if value else "失败")


def format_character_event(record: CharacterEventRecord) -> str:
    units = ",".join(f"{unit:04X}" for unit in record.utf16_units) or "-"
    return (
        f"#{record.position} 字符={record.character_repr} 类型={record.kind} "
        f"码点={','.join(record.codepoints) or '-'} UTF16={units} "
        f"方式={record.method} 请求事件={record.requested} 接受事件={record.accepted} "
        f"按下={_format_flag(record.key_down_success)} 释放={_format_flag(record.key_up_success)} "
        f"状态={record.status}"
        + (f" 错误={record.error}" if record.error else "")
    )


def format_character_event_data(data: dict) -> str:
    return format_character_event(
        CharacterEventRecord(
            position=data["position"],
            character_repr=data["character_repr"],
            codepoints=tuple(data["codepoints"]),
            utf16_units=tuple(int(unit, 16) for unit in data["utf16_units"]),
            kind=data["kind"],
            method=data["method"],
            requested=data["requested"],
            accepted=data["accepted"],
            key_down_success=data["key_down_success"],
            key_up_success=data["key_up_success"],
            status=data["status"],
            error=data.get("error"),
        )
    )


def is_chinese_char(char: str) -> bool:
    code = ord(char)
    if (
        0x3400 <= code <= 0x4DBF
        or 0x4E00 <= code <= 0x9FFF
        or 0xF900 <= code <= 0xFAFF
        or 0x20000 <= code <= 0x323AF
    ):
        return True
    name = unicodedata.name(char, "")
    return name.startswith("CJK UNIFIED IDEOGRAPH") or name.startswith(
        "CJK COMPATIBILITY IDEOGRAPH"
    )


def content_counts(text: str) -> dict[str, int]:
    text = normalize_newlines(text)
    return {
        "total": len(text),
        "english": sum(char in string.ascii_letters for char in text),
        "chinese": sum(is_chinese_char(char) for char in text),
        "spaces": text.count(" "),
        "tabs": text.count("\t"),
        "newlines": text.count("\n"),
    }


def analyze_text(text: str) -> dict:
    """返回独立解析模块的完整快照，分析过程不改写输入文本。"""
    parsed = parse_text(text)
    lines = parsed.as_dict()["lines"]
    return {
        **content_counts(parsed.normalized_text),
        **parsed.as_dict(),
        "line_count": parsed.line_count,
        "ends_with_newline": parsed.ends_with_newline,
        "line_indents": [
            {
                "spaces": line.leading_spaces,
                "tabs": line.leading_tabs,
                "characters": line.leading_whitespace_count,
            }
            for line in parsed.lines
        ],
        "trailing_spaces": [line.trailing_spaces for line in parsed.lines],
    }


def visible_whitespace(text: str) -> str:
    """生成仅供显示的空白视图，不改变实际输入文本。"""
    return normalize_newlines(text).replace("\t", "→").replace(" ", "·").replace("\n", "↵\n")


def format_line_analysis(analysis: dict) -> str:
    """显示逐行真实字符序列；标记只用于展示，不参与输入。"""
    return format_parsed_line_analysis(parse_text(analysis["normalized_text"]))


@dataclass(frozen=True)
class InputState:
    position: int
    total: int
    line_number: int
    line_content_repr: str
    leading_spaces: int
    leading_tabs: int
    sent_spaces: int
    sent_tabs: int
    sent_enters: int
    sent_unicode_characters: int
    last_character_repr: str | None
    method: str

    def as_dict(self) -> dict:
        return {
            "position": self.position,
            "total": self.total,
            "line_number": self.line_number,
            "line_content_repr": self.line_content_repr,
            "leading_spaces": self.leading_spaces,
            "leading_tabs": self.leading_tabs,
            "sent_spaces": self.sent_spaces,
            "sent_tabs": self.sent_tabs,
            "sent_enters": self.sent_enters,
            "sent_unicode_characters": self.sent_unicode_characters,
            "last_character_repr": self.last_character_repr,
            "method": self.method,
        }


def make_input_state(
    parsed: ParsedText,
    line_numbers: tuple[int, ...],
    position: int,
    sent_spaces: int,
    sent_tabs: int,
    sent_enters: int,
    sent_unicode_characters: int,
    method: str,
) -> InputState:
    total = len(parsed.normalized_text)
    if not total:
        line_number = 0
    elif position >= total and parsed.ends_with_newline:
        line_number = parsed.line_count
    else:
        index = min(max(position, 0), total - 1)
        line_number = line_numbers[index]
    line = parsed.lines[line_number - 1] if line_number else None
    last_character = (
        repr(parsed.normalized_text[position - 1]) if 0 < position <= total else None
    )
    return InputState(
        position=position,
        total=total,
        line_number=line_number,
        line_content_repr=repr(line.content) if line else "",
        leading_spaces=line.leading_spaces if line else 0,
        leading_tabs=line.leading_tabs if line else 0,
        sent_spaces=sent_spaces,
        sent_tabs=sent_tabs,
        sent_enters=sent_enters,
        sent_unicode_characters=sent_unicode_characters,
        last_character_repr=last_character,
        method=method,
    )


def format_input_state(state: InputState) -> str:
    return (
        f"位置 {state.position}/{state.total}；第 {state.line_number} 行；"
        f"该行原文 {state.line_content_repr}；"
        f"该行原始缩进 空格 {state.leading_spaces}/Tab {state.leading_tabs}；"
        f"实际发送 空格 {state.sent_spaces}/Tab {state.sent_tabs}/"
        f"Enter {state.sent_enters}/其他Unicode字符 {state.sent_unicode_characters}；"
        f"最后字符 {state.last_character_repr}；方式 {state.method}"
    )


def format_state_data(state: dict | None) -> str:
    if not state:
        return "无字符状态"
    return (
        f"位置 {state['position']}/{state['total']}；第 {state['line_number']} 行；"
        f"该行原文 {state['line_content_repr']}；"
        f"原始缩进 空格 {state['leading_spaces']}/Tab {state['leading_tabs']}；"
        f"实际发送 空格 {state['sent_spaces']}/Tab {state['sent_tabs']}/"
        f"Enter {state['sent_enters']}/其他Unicode字符 {state['sent_unicode_characters']}；"
        f"最后字符 {state['last_character_repr']}；方式 {state['method']}"
    )


def format_verification(verification: Verification) -> str:
    comparison = verification.comparison or {}
    parts = [
        f"结果校验 {verification.status}；{verification.reason}；方式 {verification.method}"
    ]
    if verification.readable:
        parts.extend(
            [
                f"字符 预期 {comparison.get('expected_total')}/实际 {comparison.get('actual_total')}",
                f"行数 预期 {comparison.get('expected_lines')}/实际 {comparison.get('actual_lines')}",
                f"空格 预期 {comparison.get('expected_spaces')}/实际 {comparison.get('actual_spaces')}",
                f"Tab 预期 {comparison.get('expected_tabs')}/实际 {comparison.get('actual_tabs')}",
                f"换行 预期 {comparison.get('expected_newlines')}/实际 {comparison.get('actual_newlines')}",
                f"末尾换行 预期 {comparison.get('expected_ends_with_newline')}/实际 {comparison.get('actual_ends_with_newline')}",
            ]
        )
    if verification.focus_same is not None:
        parts.append(f"焦点保持 {'是' if verification.focus_same else '否'}")
    if verification.caret_restored is not None:
        parts.append(f"光标恢复 {'是' if verification.caret_restored else '否'}")
    return "；".join(parts)


def format_verification_data(data: dict | None) -> str:
    if not data:
        return "无结果校验记录"
    verification = Verification(
        status=data.get("status", "unverified"),
        method=data.get("method", "none"),
        reason=data.get("reason", ""),
        readable=data.get("readable", False),
        exact_match=data.get("exact_match"),
        focus_same=data.get("focus_same"),
        caret_restored=data.get("caret_restored"),
        comparison=data.get("comparison"),
    )
    return format_verification(verification)


def format_run_diagnostics(data: dict, parsed: ParsedText) -> str:
    def flag(value: bool | None) -> str:
        return "未知" if value is None else ("是" if value else "否")

    method_labels = {
        "pending": "尚未选择",
        "uia_value": "UI Automation（ValuePattern.SetValue）",
        "clipboard": "Clipboard Paste（系统剪贴板 + 单次 Ctrl+V）",
        "keyboard": "SendInput（逐字符模拟键盘输入）",
    }
    clipboard_labels = {
        "not_used": "未使用",
        "success": "是，并已读回核对完整文本",
        "failed": "否",
    }
    paste_labels = {"not_used": "否", "sent": "是", "failed": "已尝试但系统未确认全部事件"}
    return "\n".join(
        [
            f"请求策略：{data['requested_strategy']}",
            f"输入方式：{method_labels.get(data['method'], data['method'])}",
            f"目标捕获阶段：{data.get('target_capture_phase', 'not_started')}",
            f"目标控件是否成功获取：{flag(data['target_captured'])}",
            f"目标文本是否可读取：{flag(data['target_readable'])}",
            f"ValuePattern 是否存在：{flag(data['value_pattern_supported'])}",
            f"ValuePattern 是否只读：{flag(data['value_read_only'])}",
            f"目标控件是否支持整值写入：{flag(data['direct_writable'])}",
            f"剪贴板是否成功写入：{clipboard_labels[data['clipboard_write']]}",
            f"是否执行 Ctrl+V：{paste_labels[data['paste_shortcut']]}",
            f"是否执行逐字符输入：{'是' if data['keyboard_input'] else '否'}",
            f"是否执行 Enter：{'是' if data['enter_input'] else '否'}",
            f"空格方式：{data.get('space_mode', SPACE_MODE_VIRTUAL)}",
            f"Tab 方式：{data.get('tab_mode', TAB_MODE_VIRTUAL)}",
            f"换行方式：{data.get('newline_mode', NEWLINE_MODE_ENTER)}",
            f"前台焦点保持：{flag(data.get('focus_preserved'))}",
            f"输入前前台窗口/焦点：{data.get('foreground_before')}",
            f"输入后前台窗口/焦点：{data.get('foreground_after')}",
            f"SendInput 请求/接受事件：{data.get('sendinput_requested', 0)}/{data.get('sendinput_accepted', 0)}",
            f"失败字符位置：{data.get('failed_character_position') or '无'}",
            f"原始 SHA-256：{parsed.sha256}",
            f"原始字符总数：{len(parsed.normalized_text)}",
            f"原始空格数量：{parsed.spaces}",
            f"原始 Tab 数量：{parsed.tabs}",
            f"原始换行数量：{parsed.newline_count}",
            f"原始空行数量：{parsed.blank_line_count}",
            f"最大连续空行：{parsed.max_consecutive_blank_lines}",
            f"末尾换行：{'是' if parsed.ends_with_newline else '否'}",
            f"最终验证状态：{data['verification_status']}",
        ]
    )


def _require_windows_input() -> None:
    if not IS_WINDOWS:
        raise OSError("当前平台不支持 Windows 键盘模拟；文本分析和单元测试仍可使用。")
    if WINDOWS_API_ERROR is not None:
        raise OSError(WINDOWS_API_ERROR)
    if user32 is None:
        raise OSError("Windows 键盘输入 API 未初始化。")


def _keyboard_event(vk: int, scan: int, flags: int) -> ctypes.Structure:
    _require_windows_input()
    return INPUT(
        INPUT_KEYBOARD,
        INPUT_UNION(
            ki=KEYBDINPUT(
                wVk=vk,
                wScan=scan,
                dwFlags=flags,
                time=0,
                dwExtraInfo=0,
            )
        ),
    )


def _raw_send_input(events: tuple[ctypes.Structure, ...]) -> int:
    if not events:
        return 0
    event_array = (INPUT * len(events))(*events)
    return int(user32.SendInput(len(events), event_array, ctypes.sizeof(INPUT)))


def _pressed_keys(events: tuple[ctypes.Structure, ...]) -> list[tuple[int, int, int]]:
    pressed: dict[tuple[int, int], int] = {}
    for event in events:
        key = event.data.ki
        identity = (int(key.wVk), int(key.wScan))
        if key.dwFlags & KEYEVENTF_KEYUP:
            pressed.pop(identity, None)
        else:
            pressed[identity] = int(key.dwFlags)
    return [(vk, scan, flags) for (vk, scan), flags in reversed(list(pressed.items()))]


def _send_input(events: tuple[ctypes.Structure, ...]) -> int:
    _require_windows_input()
    if not events:
        raise ValueError("SendInput 事件列表不能为空")
    requested = len(events)
    sent = _raw_send_input(events)
    if sent == requested:
        return sent

    accepted = min(max(sent, 0), requested)
    error_code = ctypes.get_last_error()
    detail = ctypes.FormatError(error_code) if error_code else "系统未接受全部模拟键盘事件"
    pressed = _pressed_keys(events[:accepted])
    cleanup = tuple(
        _keyboard_event(vk, scan, flags | KEYEVENTF_KEYUP)
        for vk, scan, flags in pressed
    )
    cleanup_accepted = 0
    cleanup_error: str | None = None
    if cleanup:
        try:
            cleanup_accepted = _raw_send_input(cleanup)
        except Exception as exc:
            cleanup_error = str(exc)
    recovered = not pressed or cleanup_accepted == len(cleanup)
    accepted_prefix = events[:accepted]
    key_down_count = sum(
        not (event.data.ki.dwFlags & KEYEVENTF_KEYUP) for event in events
    )
    accepted_down_count = sum(
        not (event.data.ki.dwFlags & KEYEVENTF_KEYUP) for event in accepted_prefix
    )
    result = EventBatchResult(
        requested=requested,
        accepted=accepted,
        key_down=accepted_down_count == key_down_count,
        key_up=accepted == requested,
        recovered=recovered,
        error=detail if cleanup_error is None else f"{detail}；清理异常：{cleanup_error}",
    )
    message = f"SendInput 请求 {requested} 个事件，仅接受 {accepted} 个：{detail}"
    if pressed:
        message += (
            f"；已尝试释放 {len(pressed)} 个遗留按键，清理接受 {cleanup_accepted}/{len(cleanup)}"
            if recovered
            else "；遗留按键清理失败，请手动松开相关按键"
        )
    if cleanup_error:
        message += f"；清理异常：{cleanup_error}"
    raise SendInputError(message, result, error_code or None)


def send_key(virtual_key: int) -> EventBatchResult:
    """发送一个虚拟键的按下与松开事件。"""
    events = (
        _keyboard_event(virtual_key, 0, 0),
        _keyboard_event(virtual_key, 0, KEYEVENTF_KEYUP),
    )
    accepted = _send_input(events)
    return EventBatchResult(
        requested=len(events),
        accepted=accepted,
        key_down=True,
        key_up=True,
    )


def send_paste_shortcut() -> EventBatchResult:
    events = (
        _keyboard_event(VK_CONTROL, 0, KEYEVENTF_KEYUP),
        _keyboard_event(VK_V, 0, KEYEVENTF_KEYUP),
        _keyboard_event(VK_CONTROL, 0, 0),
        _keyboard_event(VK_V, 0, 0),
        _keyboard_event(VK_V, 0, KEYEVENTF_KEYUP),
        _keyboard_event(VK_CONTROL, 0, KEYEVENTF_KEYUP),
    )
    accepted = _send_input(events)
    return EventBatchResult(
        requested=len(events),
        accepted=accepted,
        key_down=True,
        key_up=True,
    )


def utf16_units(text: str) -> list[int]:
    """按 UTF-16-LE 拆分码元，确保 Emoji 代理项不会被截断。"""
    data = text.encode("utf-16-le")
    return [data[index] | (data[index + 1] << 8) for index in range(0, len(data), 2)]


def send_unicode_character(char: str) -> CharacterSendResult:
    if len(char) != 1:
        raise ValueError("一次只能发送一个 Unicode 字符")
    units = tuple(utf16_units(char))
    events = tuple(
        event
        for unit in units
        for event in (
            _keyboard_event(0, unit, KEYEVENTF_UNICODE),
            _keyboard_event(0, unit, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP),
        )
    )
    accepted = _send_input(events)
    return CharacterSendResult(
        character=char,
        method="unicode",
        utf16_units=units,
        batches=(
            EventBatchResult(
                requested=len(events),
                accepted=accepted,
                key_down=True,
                key_up=True,
            ),
        ),
    )


def send_printable_character(char: str) -> CharacterSendResult:
    return send_unicode_character(char)


def _key_character_result(char: str, method: str, result: EventBatchResult) -> CharacterSendResult:
    return CharacterSendResult(
        character=char,
        method=method,
        utf16_units=tuple(utf16_units(char)),
        batches=(result,),
    )


def send_space(mode: str = SPACE_MODE_VIRTUAL) -> CharacterSendResult:
    if mode == SPACE_MODE_UNICODE:
        return send_unicode_character(" ")
    if mode == SPACE_MODE_VIRTUAL:
        return _key_character_result(" ", "vk_space", send_key(VK_SPACE))
    raise ValueError(f"未知空格输入方式：{mode}")


def send_tab(mode: str = TAB_MODE_VIRTUAL) -> CharacterSendResult:
    if mode == TAB_MODE_VIRTUAL:
        return _key_character_result("\t", "vk_tab", send_key(VK_TAB))
    if mode == TAB_MODE_UNICODE:
        result = send_unicode_character("\t")
        return CharacterSendResult(
            character="\t",
            method="unicode_tab",
            utf16_units=result.utf16_units,
            batches=result.batches,
        )
    raise ValueError(f"未知 Tab 输入方式：{mode}")


def send_newline(mode: str = NEWLINE_MODE_ENTER) -> CharacterSendResult:
    if mode == NEWLINE_MODE_ENTER:
        return _key_character_result("\n", "vk_return", send_key(VK_RETURN))
    if mode == NEWLINE_MODE_UNICODE_LF:
        result = send_unicode_character("\n")
        return CharacterSendResult(
            character="\n",
            method="unicode_lf",
            utf16_units=result.utf16_units,
            batches=result.batches,
        )
    raise ValueError(f"未知换行输入方式：{mode}")


def send_character(
    char: str,
    literal_mode: bool | None = None,
    options: CharacterOptions | None = None,
) -> CharacterSendResult:
    """默认使用记事本实测支持的实体空格、实体 Tab 和真实 Enter。"""
    if len(char) != 1:
        raise ValueError("一次只能调度一个字符")
    if literal_mode is not None:
        options = CharacterOptions(
            space_mode=SPACE_MODE_UNICODE if literal_mode else SPACE_MODE_VIRTUAL,
            tab_mode=TAB_MODE_UNICODE if literal_mode else TAB_MODE_VIRTUAL,
            newline_mode=NEWLINE_MODE_ENTER,
        )
    options = options or CharacterOptions()
    if char in ("\n", "\r"):
        return send_newline(options.newline_mode)
    if char == " ":
        return send_space(options.space_mode)
    if char == "\t":
        return send_tab(options.tab_mode)
    return send_printable_character(char)


def get_foreground_window_info() -> dict | None:
    if not IS_WINDOWS or user32 is None:
        return None
    foreground = int(user32.GetForegroundWindow())
    if not foreground:
        return {"foreground_window": 0, "process_id": None, "focus_window": None}
    process_id = wintypes.DWORD()
    thread_id = user32.GetWindowThreadProcessId(foreground, ctypes.byref(process_id))
    info = GUITHREADINFO()
    info.cbSize = ctypes.sizeof(info)
    focus = None
    if thread_id and user32.GetGUIThreadInfo(thread_id, ctypes.byref(info)):
        focus = int(info.hwndFocus) if info.hwndFocus else None
    return {
        "foreground_window": foreground,
        "process_id": int(process_id.value),
        "focus_window": focus,
    }


def is_f8_pressed() -> bool:
    _require_windows_input()
    return bool(user32.GetAsyncKeyState(VK_F8) & 0x8000)


def _dispatch_method(char: str, options: CharacterOptions) -> str:
    if char in ("\n", "\r"):
        return "vk_return" if options.newline_mode == NEWLINE_MODE_ENTER else "unicode_lf"
    if char == " ":
        return "unicode" if options.space_mode == SPACE_MODE_UNICODE else "vk_space"
    if char == "\t":
        return "vk_tab" if options.tab_mode == TAB_MODE_VIRTUAL else "unicode_tab"
    return "unicode"


def _character_event_record(
    position: int,
    char: str,
    result: CharacterSendResult | None,
    method: str,
    status: str,
    error: str | None = None,
) -> CharacterEventRecord:
    return CharacterEventRecord(
        position=position,
        character_repr=repr(char),
        codepoints=tuple(f"U+{ord(value):04X}" for value in char),
        utf16_units=tuple(utf16_units(char)),
        kind=_character_kind(char),
        method=result.method if result is not None else method,
        requested=result.requested if result is not None else None,
        accepted=result.accepted if result is not None else None,
        key_down_success=result.key_down_success if result is not None else None,
        key_up_success=result.key_up_success if result is not None else None,
        status=status,
        error=error,
    )


@_exclusive_input
def type_text(
    text: str,
    interval_ms: int,
    stop_event: threading.Event | None = None,
    should_stop: Callable[[], bool] | None = None,
    on_progress: Callable[[int], None] | None = None,
    literal_mode: bool | None = None,
    on_state: Callable[[InputState], None] | None = None,
    *,
    space_mode: str = SPACE_MODE_VIRTUAL,
    tab_mode: str = TAB_MODE_VIRTUAL,
    newline_mode: str = NEWLINE_MODE_ENTER,
    on_event: Callable[[CharacterEventRecord], None] | None = None,
) -> int:
    """按原始顺序调度字符；只统计完整成功发送的 Python 字符。"""
    parsed = parse_text(text)
    text = parsed.normalized_text
    line_numbers = parsed.character_line_numbers()
    options = CharacterOptions(space_mode, tab_mode, newline_mode)
    if literal_mode is not None:
        options = CharacterOptions(
            space_mode=SPACE_MODE_UNICODE if literal_mode else SPACE_MODE_VIRTUAL,
            tab_mode=TAB_MODE_UNICODE if literal_mode else TAB_MODE_VIRTUAL,
            newline_mode=NEWLINE_MODE_ENTER,
        )
    completed = 0
    sent_spaces = 0
    sent_tabs = 0
    sent_enters = 0
    sent_unicode_characters = 0
    interval = max(0, interval_ms) / 1000

    def abort_requested() -> bool:
        return (stop_event is not None and stop_event.is_set()) or (
            should_stop is not None and should_stop()
        )

    def wait(duration: float) -> bool:
        if stop_event is not None:
            return stop_event.wait(duration)
        time.sleep(duration)
        return False

    if on_state is not None:
        on_state(make_input_state(parsed, line_numbers, 0, 0, 0, 0, 0, "keyboard"))

    for index, char in enumerate(text):
        if abort_requested():
            break
        method = _dispatch_method(char, options)
        try:
            if literal_mode is not None:
                result = send_character(char, literal_mode)
            elif options == CharacterOptions():
                result = send_character(char)
            else:
                result = send_character(char, options=options)
        except SendInputError as exc:
            if on_event is not None:
                failure_result = CharacterSendResult(
                    character=char,
                    method=method,
                    utf16_units=tuple(utf16_units(char)),
                    batches=(exc.result,),
                )
                on_event(
                    _character_event_record(
                        index + 1, char, failure_result, method, "failed", str(exc)
                    )
                )
            raise
        except Exception as exc:
            if on_event is not None:
                on_event(
                    _character_event_record(
                        index + 1, char, None, method, "failed", str(exc)
                    )
                )
            raise

        if char == " ":
            sent_spaces += 1
        elif char == "\t":
            sent_tabs += 1
        elif char == "\n":
            if result.method == NEWLINE_MODE_ENTER:
                sent_enters += 1
            else:
                sent_unicode_characters += 1
        else:
            sent_unicode_characters += 1
        completed = index + 1
        state = make_input_state(
            parsed,
            line_numbers,
            completed,
            sent_spaces,
            sent_tabs,
            sent_enters,
            sent_unicode_characters,
            "keyboard",
        )
        if on_state is not None:
            on_state(state)
        if on_event is not None:
            on_event(_character_event_record(index + 1, char, result, method, "sent"))
        if on_progress is not None:
            on_progress(completed)
        delay = max(interval, NEWLINE_SETTLE_SECONDS) if char == "\n" else interval
        if delay and wait(delay):
            break

    return completed


@_exclusive_input
def input_worker(
    text: str,
    countdown_seconds: int,
    interval_ms: int,
    stop_event: threading.Event,
    events: queue.Queue[dict],
    literal_mode: bool | None = None,
    target_factory: Callable[[], TargetController] | None = None,
    strategy: str = INPUT_STRATEGY_KEYBOARD,
    paste_settle_seconds: float = PASTE_SETTLE_SECONDS,
    verification_timeout: float = PASTE_VERIFY_TIMEOUT_SECONDS,
    space_mode: str = SPACE_MODE_VIRTUAL,
    tab_mode: str = TAB_MODE_VIRTUAL,
    newline_mode: str = NEWLINE_MODE_ENTER,
) -> None:
    """默认独立逐键输入；整值直写或粘贴仅在显式策略下执行。"""
    if strategy not in (INPUT_STRATEGY_AUTO, INPUT_STRATEGY_KEYBOARD):
        raise ValueError(f"未知输入策略：{strategy}")
    if literal_mode is not None:
        space_mode = SPACE_MODE_UNICODE if literal_mode else SPACE_MODE_VIRTUAL
        tab_mode = TAB_MODE_UNICODE if literal_mode else TAB_MODE_VIRTUAL
        newline_mode = NEWLINE_MODE_ENTER

    completed = 0
    latest_state: InputState | None = None
    verification = Verification(
        status="unverified",
        method="none",
        reason="输入尚未完成",
    )
    target = None
    target_error: str | None = None
    debug_lines: deque[str] = deque(maxlen=200)
    character_events: deque[CharacterEventRecord] = deque(maxlen=MAX_CHARACTER_EVENTS)
    character_event_count = 0
    latest_character_event: CharacterEventRecord | None = None
    foreground_before: dict | None = None
    foreground_after: dict | None = None
    diagnostics = {
        "requested_strategy": strategy,
        "method": "pending",
        "target_capture_phase": "not_started",
        "target_captured": False,
        "target_readable": None,
        "value_pattern_supported": None,
        "value_read_only": None,
        "direct_writable": None,
        "clipboard_write": "not_used",
        "paste_shortcut": "not_used",
        "keyboard_input": False,
        "enter_input": False,
        "space_mode": space_mode,
        "tab_mode": tab_mode,
        "newline_mode": newline_mode,
        "foreground_before": None,
        "foreground_after": None,
        "focus_preserved": None,
        "sendinput_requested": 0,
        "sendinput_accepted": 0,
        "failed_character_position": None,
        "verification_status": "unverified",
    }
    clipboard_backup: ClipboardSnapshot | None = None
    clipboard_write_attempted = False
    clipboard_sequence: int | None = None
    paste_attempted = False
    terminal_emitted = False
    parsed = parse_text("")
    line_numbers: tuple[int, ...] = ()

    def emit_terminal(event: dict) -> None:
        nonlocal terminal_emitted, foreground_after
        if terminal_emitted:
            return
        if foreground_before is not None:
            try:
                foreground_after = get_foreground_window_info()
            except Exception as exc:
                debug_lines.append(f"读取输入后前台焦点失败：{exc}")
            diagnostics["foreground_after"] = foreground_after
            if foreground_after is not None:
                before_focus = foreground_before.get("focus_window")
                after_focus = foreground_after.get("focus_window")
                diagnostics["focus_preserved"] = (
                    before_focus == after_focus
                    if before_focus is not None and after_focus is not None
                    else foreground_before.get("foreground_window")
                    == foreground_after.get("foreground_window")
                )
        if clipboard_backup is not None and clipboard_write_attempted:
            try:
                _restored, reason = restore_clipboard_text(
                    clipboard_backup, clipboard_sequence
                )
            except Exception as exc:
                reason = f"恢复原剪贴板文本失败：{exc}"
            debug_lines.append(f"剪贴板恢复：{reason}")
        diagnostics["verification_status"] = event["verification"]["status"]
        event["state"] = event.get("state") or (
            latest_state.as_dict() if latest_state else None
        )
        event["source"] = {
            "sha256": parsed.sha256,
            "total": len(parsed.normalized_text),
            "spaces": parsed.spaces,
            "tabs": parsed.tabs,
            "newlines": parsed.newline_count,
            "blank_lines": parsed.blank_line_count,
            "max_consecutive_blank_lines": parsed.max_consecutive_blank_lines,
            "line_count": parsed.line_count,
            "ends_with_newline": parsed.ends_with_newline,
        }
        event["diagnostics"] = dict(diagnostics)
        event["character_events"] = [record.as_dict() for record in character_events]
        event["character_event_count"] = character_event_count
        event["character_events_truncated"] = max(
            0, character_event_count - len(character_events)
        )
        event["log"] = [format_run_diagnostics(diagnostics, parsed), *debug_lines]
        terminal_emitted = True
        events.put(event)

    try:
        parsed = parse_text(text)
        text = parsed.normalized_text
        line_numbers = parsed.character_line_numbers()
        if "\x00" in text:
            raise ValueError("原始文本包含 NUL，无法安全输入")
        text.encode("utf-16-le")
        latest_state = make_input_state(
            parsed, line_numbers, 0, 0, 0, 0, 0, "pending"
        )
        if not IS_WINDOWS:
            raise OSError("此工具目前仅支持 Windows；文本分析和单元测试不受影响。")

        def abort_requested() -> bool:
            return stop_event.is_set() or is_f8_pressed()

        def block_state(position: int, method: str) -> InputState:
            return make_input_state(parsed, line_numbers, position, 0, 0, 0, 0, method)

        def wait_interruptible(duration: float) -> bool:
            deadline = time.monotonic() + max(0, duration)
            while True:
                if abort_requested():
                    return True
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                stop_event.wait(min(0.05, remaining))

        deadline = time.monotonic() + countdown_seconds
        last_second = None
        while True:
            if abort_requested():
                verification = Verification(
                    status="unverified", method="none", reason="倒计时期间已停止"
                )
                emit_terminal(
                    {
                        "type": "stopped",
                        "count": 0,
                        "verification": verification.as_dict(),
                    }
                )
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            second = math.ceil(remaining)
            if second != last_second:
                events.put({"type": "countdown", "remaining": second})
                last_second = second
            stop_event.wait(min(0.1, remaining))

        def capture_target(phase: str) -> TargetSnapshot | None:
            nonlocal target, target_error
            diagnostics["target_capture_phase"] = phase
            try:
                target = (target_factory or TargetController)()
                captured = target.capture()
                diagnostics["target_captured"] = True
                diagnostics["target_readable"] = captured.readable
                diagnostics["value_pattern_supported"] = captured.value_pattern_supported
                diagnostics["value_read_only"] = captured.value_read_only
                diagnostics["direct_writable"] = captured.direct_writable
                events.put({"type": "target", "phase": phase, "target": captured.as_dict()})
                return captured
            except Exception as exc:
                target_error = str(exc)
                if target is not None:
                    target.close()
                target = None
                events.put(
                    {
                        "type": "target",
                        "phase": phase,
                        "target": {
                            "readable": False,
                            "direct_writable": False,
                            "value_pattern_supported": False,
                            "reason": target_error,
                        },
                    }
                )
                return None

        events.put({"type": "typing"})
        foreground_before = get_foreground_window_info()
        diagnostics["foreground_before"] = foreground_before
        snapshot = None
        if strategy == INPUT_STRATEGY_KEYBOARD:
            diagnostics["method"] = "keyboard"
            diagnostics["keyboard_input"] = True
            diagnostics["target_capture_phase"] = "after_input"
        else:
            snapshot = capture_target("before_input")
        events.put(
            {
                "type": "diagnostic",
                "diagnostics": dict(diagnostics),
                "text": format_run_diagnostics(diagnostics, parsed),
                "log": list(debug_lines),
            }
        )

        if abort_requested():
            verification = Verification(
                status="unverified", method="none", reason="输入开始前已停止"
            )
            emit_terminal(
                {
                    "type": "stopped",
                    "count": 0,
                    "verification": verification.as_dict(),
                }
            )
            return

        report_every = max(1, len(text) // 200)

        def report_progress(count: int) -> None:
            nonlocal completed
            completed = count
            if count % report_every == 0 or count == len(text):
                events.put(
                    {
                        "type": "progress",
                        "count": count,
                        "state": latest_state.as_dict() if latest_state else None,
                        "character": (
                            latest_character_event.as_dict()
                            if latest_character_event
                            else None
                        ),
                    }
                )

        def report_state(state: InputState) -> None:
            nonlocal latest_state
            latest_state = state
            debug_lines.append(format_input_state(state))

        def report_character(record: CharacterEventRecord) -> None:
            nonlocal character_event_count, latest_character_event
            character_events.append(record)
            character_event_count += 1
            latest_character_event = record
            if record.requested is not None:
                diagnostics["sendinput_requested"] += record.requested
            if record.accepted is not None:
                diagnostics["sendinput_accepted"] += record.accepted
            if record.status == "failed":
                diagnostics["failed_character_position"] = record.position
            debug_lines.append(format_character_event(record))

        if strategy == INPUT_STRATEGY_AUTO and snapshot is not None and snapshot.direct_writable:
            diagnostics["method"] = "uia_value"
            verification = target.write_exact(text)
            completed = len(text)
            latest_state = block_state(completed, "uia_value")
            debug_lines.append(format_input_state(latest_state))
            events.put(
                {
                    "type": "progress",
                    "count": completed,
                    "state": latest_state.as_dict(),
                }
            )
        elif strategy == INPUT_STRATEGY_KEYBOARD:
            diagnostics["method"] = "keyboard"
            diagnostics["keyboard_input"] = True
            completed = type_text(
                text=text,
                interval_ms=interval_ms,
                stop_event=stop_event,
                should_stop=is_f8_pressed,
                on_progress=report_progress,
                on_state=report_state,
                literal_mode=literal_mode,
                space_mode=space_mode,
                tab_mode=tab_mode,
                newline_mode=newline_mode,
                on_event=report_character,
            )
            diagnostics["enter_input"] = bool(
                latest_state and latest_state.sent_enters
            )
            if completed >= len(text):
                if abort_requested():
                    verification = Verification(
                        status="unverified",
                        method="keyboard",
                        reason="逐字符事件已提交，但结果比较前已停止",
                    )
                else:
                    captured = capture_target("after_input")
                    if captured is None:
                        verification = Verification(
                            status="unverified",
                            method="keyboard",
                            reason=(
                                f"无法连接目标控件：{target_error}；逐字符事件已提交，最终文本无法验证"
                                if target_error
                                else "逐字符事件已提交，但最终文本无法验证"
                            ),
                        )
                    elif captured.text is None:
                        verification = Verification(
                            status="unverified",
                            method="keyboard",
                            reason="目标不支持完整文本读取；逐字符事件已提交，最终文本无法验证",
                        )
                    elif captured.text == "" and text:
                        verification = target.verify(text, method="keyboard")
                    else:
                        comparison = compare_text(text, captured.text)
                        verification = Verification(
                            status="unverified",
                            method="keyboard",
                            reason=(
                                "输入前未读取目标基线，无法确认原前缀、后缀和选区；"
                                "当前读回仅作诊断，不能作为完整插入结果"
                            ),
                            readable=True,
                            exact_match=comparison["exact_match"],
                            comparison=comparison,
                        )
            else:
                verification = Verification(
                    status="unverified",
                    method="keyboard",
                    reason=f"输入在第 {completed} 个字符后停止，未执行结果比较",
                )
        else:
            diagnostics["method"] = "clipboard"
            clipboard_backup = capture_clipboard_text()
            clipboard_write_attempted = True
            try:
                clipboard_sequence = write_clipboard_text(text)
                if read_clipboard_text() != text:
                    raise OSError("剪贴板写入后读回与原始快照不一致")
            except Exception:
                diagnostics["clipboard_write"] = "failed"
                raise
            diagnostics["clipboard_write"] = "success"
            events.put(
                {
                    "type": "diagnostic",
                    "diagnostics": dict(diagnostics),
                    "text": format_run_diagnostics(diagnostics, parsed),
                    "log": list(debug_lines),
                }
            )
            if abort_requested():
                verification = Verification(
                    status="unverified",
                    method="clipboard",
                    reason="剪贴板已写入，但 Ctrl+V 发送前已停止",
                )
                emit_terminal(
                    {
                        "type": "stopped",
                        "count": 0,
                        "verification": verification.as_dict(),
                    }
                )
                return

            paste_attempted = True
            try:
                send_paste_shortcut()
            except Exception:
                diagnostics["paste_shortcut"] = "failed"
                raise
            diagnostics["paste_shortcut"] = "sent"
            completed = len(text)
            latest_state = block_state(completed, "clipboard")
            debug_lines.append(format_input_state(latest_state))
            events.put(
                {
                    "type": "progress",
                    "count": completed,
                    "state": latest_state.as_dict(),
                }
            )
            events.put(
                {
                    "type": "diagnostic",
                    "diagnostics": dict(diagnostics),
                    "text": format_run_diagnostics(diagnostics, parsed),
                    "log": list(debug_lines),
                }
            )

            if wait_interruptible(paste_settle_seconds):
                verification = Verification(
                    status="unverified",
                    method="clipboard",
                    reason="Ctrl+V 已发送，但等待目标处理期间停止，无法确认最终文本",
                )
                emit_terminal(
                    {
                        "type": "stopped",
                        "count": completed,
                        "verification": verification.as_dict(),
                    }
                )
                return

            expected = target.expected_text(text) if target is not None else None
            if expected is None:
                reason = (
                    f"无法连接目标控件：{target_error}；整段粘贴已发送，最终文本无法验证"
                    if target_error
                    else "目标不支持完整文本读取或选区定位；整段粘贴已发送，最终文本无法验证"
                )
                verification = Verification(
                    status="unverified", method="clipboard", reason=reason
                )
            else:
                verify_deadline = time.monotonic() + max(0, verification_timeout)
                while True:
                    if abort_requested():
                        verification = Verification(
                            status="unverified",
                            method="clipboard",
                            reason="整段粘贴已发送，但读回比较前停止，无法确认最终文本",
                        )
                        emit_terminal(
                            {
                                "type": "stopped",
                                "count": completed,
                                "verification": verification.as_dict(),
                            }
                        )
                        return
                    try:
                        verification = target.verify(expected, method="clipboard")
                    except Exception as exc:
                        verification = Verification(
                            status="unverified",
                            method="clipboard",
                            reason=f"目标读回失败：{exc}",
                        )
                    if verification.status == "verified":
                        break
                    remaining = verify_deadline - time.monotonic()
                    if remaining <= 0:
                        if verification.status == "mismatch":
                            verification = Verification(
                                status="mismatch",
                                method="clipboard",
                                reason=(
                                    f"整段粘贴已发送；等待 {verification_timeout:g} 秒后"
                                    "目标读回仍与预期不一致"
                                ),
                                readable=verification.readable,
                                exact_match=False,
                                focus_same=verification.focus_same,
                                comparison=verification.comparison,
                            )
                        break
                    stop_event.wait(min(0.05, remaining))

        final_state = latest_state or block_state(completed, diagnostics["method"])
        diagnostics["enter_input"] = bool(
            diagnostics["keyboard_input"] and final_state.sent_enters
        )
        emit_terminal(
            {
                "type": "done" if completed >= len(text) else "stopped",
                "count": completed,
                "state": final_state.as_dict(),
                "verification": verification.as_dict(),
            }
        )
    except Exception as exc:
        message = str(exc)
        if diagnostics["keyboard_input"] and verification.method == "none":
            verification = Verification(
                status="unverified",
                method="keyboard",
                reason=f"逐字符输入未完成：{message}",
            )
        if target is not None and target.write_attempted:
            message += "；直写已经尝试，目标可能已改变，请勿不检查就重复运行"
        if paste_attempted:
            message += "；整段粘贴已经尝试，目标可能已改变，请勿不检查就重复运行"
        emit_terminal(
            {
                "type": "error",
                "message": message,
                "count": completed,
                "state": latest_state.as_dict() if latest_state else None,
                "verification": verification.as_dict(),
            }
        )
    finally:
        if target is not None:
            target.close()


@dataclass(frozen=True)
class Scene:
    name: str
    countdown: int
    interval: int
    hint: str


SCENES = {
    "通用窗口": Scene(
        "通用窗口",
        5,
        20,
        "默认逐字符 SendInput；空格走实体 VK_SPACE，Tab 走实体 Tab，换行走真实 Enter。",
    ),
    "代码编辑器": Scene(
        "代码编辑器",
        5,
        15,
        "逐字符输入会逐行发送 Enter；目标自动缩进可能改变结果，必须读回或保存后对照。",
    ),
    "网页输入框": Scene(
        "网页输入框",
        5,
        25,
        "逐字符事件可能被网页脚本拦截或改写；Tab 可能移动焦点，完成后必须核对实际内容。",
    ),
    "终端 / 命令行": Scene(
        "终端 / 命令行",
        5,
        40,
        "逐字符 Enter 可能在终端立即执行命令；请只用于你有权操作的隔离测试终端。",
    ),
    "远程桌面 / 虚拟机": Scene(
        "远程桌面 / 虚拟机",
        7,
        80,
        "远程桌面可能延迟或丢弃键盘事件；可增大逐键间隔，并在完成后核对远端内容。",
    ),
}


class AutoTyperApp:
    BG = "#F4F6FA"
    CARD = "#FFFFFF"
    TEXT = "#1F2937"
    MUTED = "#6B7280"
    BORDER = "#E2E8F0"
    ACCENT = "#4F46E5"
    DANGER = "#DC2626"

    def __init__(self, root: tk.Tk):
        self.root = root
        self.events: queue.Queue | None = None
        self.stop_event: threading.Event | None = None
        self.worker: threading.Thread | None = None
        self.total_chars = 0

        self.editor_text = tk.StringVar()
        self.scene_name = tk.StringVar(value="通用窗口")
        self.strategy_name = tk.StringVar(value=next(iter(INPUT_STRATEGIES)))
        self.literal_mode = tk.BooleanVar(value=False)
        self.space_mode = tk.StringVar(value=next(iter(SPACE_MODES)))
        self.tab_mode = tk.StringVar(value=next(iter(TAB_MODES)))
        self.newline_mode = tk.StringVar(value=next(iter(NEWLINE_MODES)))
        self.countdown_text = tk.StringVar(value="5")
        self.interval_text = tk.StringVar(value="20")
        self.paste_settle_text = tk.StringVar(value=str(int(PASTE_SETTLE_SECONDS * 1000)))
        self.scene_hint = tk.StringVar(value=SCENES["通用窗口"].hint)
        self.stats_text = tk.StringVar()
        self.status_text = tk.StringVar(
            value="准备就绪。点击运行后，请在倒计时内切换到目标窗口并定位光标。"
        )

        self.root.title("自动键盘录入助手 V2.3")
        self.root.geometry("1040x1140")
        self.root.minsize(920, 940)
        self.root.configure(bg=self.BG)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.configure_styles()
        self.build_ui()
        self.update_strategy_controls()
        self.update_content_analysis()

    def configure_styles(self) -> None:
        style = ttk.Style(self.root)
        style.theme_use("clam")
        family = "Microsoft YaHei UI"

        style.configure("TFrame", background=self.BG)
        style.configure("Card.TFrame", background=self.CARD)
        style.configure("TLabel", background=self.BG, foreground=self.TEXT, font=(family, 10))
        style.configure("Card.TLabel", background=self.CARD, foreground=self.TEXT, font=(family, 10))
        style.configure("Muted.TLabel", background=self.BG, foreground=self.MUTED, font=(family, 9))
        style.configure("CardMuted.TLabel", background=self.CARD, foreground=self.MUTED, font=(family, 9))
        style.configure("Title.TLabel", background=self.BG, foreground=self.TEXT, font=(family, 22, "bold"))
        style.configure("Section.TLabel", background=self.CARD, foreground=self.TEXT, font=(family, 11, "bold"))
        style.configure("TButton", font=(family, 10), padding=(12, 7))
        style.configure("Secondary.TButton", font=(family, 10), padding=(12, 7))
        style.configure(
            "Accent.TButton",
            font=(family, 11, "bold"),
            padding=(24, 10),
            background=self.ACCENT,
            foreground="#FFFFFF",
            borderwidth=0,
        )
        style.map("Accent.TButton", background=[("active", "#4338CA"), ("disabled", "#A5B4FC")])
        style.configure(
            "Danger.TButton",
            font=(family, 11, "bold"),
            padding=(24, 10),
            background=self.DANGER,
            foreground="#FFFFFF",
            borderwidth=0,
        )
        style.map("Danger.TButton", background=[("active", "#B91C1C"), ("disabled", "#FCA5A5")])
        style.configure("TCombobox", font=(family, 10), padding=5)
        style.configure("TSpinbox", font=(family, 10), padding=5)
        style.configure(
            "Card.TCheckbutton",
            background=self.CARD,
            foreground=self.TEXT,
            font=(family, 10),
        )
        style.configure("Horizontal.TProgressbar", background=self.ACCENT, troughcolor=self.BORDER)

    def build_ui(self) -> None:
        outer = ttk.Frame(self.root, style="TFrame", padding=(24, 20))
        outer.pack(fill="both", expand=True)

        ttk.Label(outer, text="自动键盘录入助手 V2.3", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            outer,
            text="默认按原始顺序逐字符 SendInput；整值直写或整段粘贴仅在你明确选择时启用。事件提交成功不等于目标文本一致。",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(4, 16))

        body = ttk.Frame(outer, style="TFrame")
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=3)
        body.rowconfigure(1, weight=0)
        body.rowconfigure(2, weight=3)
        body.rowconfigure(3, weight=0)

        editor_card = ttk.Frame(body, style="Card.TFrame", padding=16)
        editor_card.grid(row=0, column=0, sticky="nsew", pady=(0, 12))
        editor_card.columnconfigure(0, weight=1)

        editor_header = ttk.Frame(editor_card, style="Card.TFrame")
        editor_header.grid(row=0, column=0, sticky="ew")
        editor_header.columnconfigure(0, weight=1)
        ttk.Label(editor_header, text="待输入内容", style="Section.TLabel").grid(row=0, column=0, sticky="w")
        self.read_button = ttk.Button(
            editor_header,
            text="读取系统剪切板",
            style="Secondary.TButton",
            command=self.read_clipboard,
        )
        self.read_button.grid(row=0, column=1, sticky="e")
        ttk.Label(
            editor_header,
            text="可直接在此处按 Ctrl+V 粘贴",
            style="CardMuted.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=(3, 8))

        text_frame = ttk.Frame(editor_card, style="Card.TFrame")
        text_frame.grid(row=1, column=0, sticky="nsew", pady=(0, 8))
        text_frame.columnconfigure(0, weight=1)
        text_frame.rowconfigure(0, weight=1)

        self.editor = tk.Text(
            text_frame,
            height=8,
            wrap="none",
            undo=True,
            font=("Consolas", 11),
            bg="#FFFFFF",
            fg=self.TEXT,
            insertbackground=self.ACCENT,
            relief="flat",
            highlightthickness=1,
            highlightbackground=self.BORDER,
            highlightcolor=self.ACCENT,
            padx=10,
            pady=8,
        )
        self.editor.grid(row=0, column=0, sticky="nsew")
        editor_vbar = ttk.Scrollbar(text_frame, orient="vertical", command=self.editor.yview)
        editor_vbar.grid(row=0, column=1, sticky="ns")
        editor_hbar = ttk.Scrollbar(text_frame, orient="horizontal", command=self.editor.xview)
        editor_hbar.grid(row=1, column=0, sticky="ew")
        self.editor.configure(yscrollcommand=editor_vbar.set, xscrollcommand=editor_hbar.set)
        self.editor.bind("<KeyRelease>", lambda _event: self.update_content_analysis())
        self.editor.bind("<<Paste>>", lambda _event: self.root.after_idle(self.update_content_analysis))
        self.editor.bind("<<Cut>>", lambda _event: self.root.after_idle(self.update_content_analysis))
        self.editor.bind("<Control-a>", self.select_all)

        ttk.Label(editor_card, textvariable=self.stats_text, style="CardMuted.TLabel").grid(
            row=2, column=0, sticky="w"
        )

        settings_card = ttk.Frame(body, style="Card.TFrame", padding=16)
        settings_card.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        settings_card.columnconfigure(0, weight=0)
        settings_card.columnconfigure(1, weight=1)
        settings_card.columnconfigure(2, weight=0)

        ttk.Label(settings_card, text="输入场景", style="Section.TLabel").grid(row=0, column=0, sticky="w")
        self.scene_combo = ttk.Combobox(
            settings_card,
            textvariable=self.scene_name,
            values=list(SCENES),
            state="readonly",
            width=24,
        )
        self.scene_combo.grid(row=0, column=1, sticky="w", padx=(10, 0))
        self.scene_combo.bind("<<ComboboxSelected>>", self.apply_scene)
        ttk.Label(
            settings_card,
            textvariable=self.scene_hint,
            style="CardMuted.TLabel",
            wraplength=900,
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(6, 12))

        ttk.Label(settings_card, text="输入策略", style="Section.TLabel").grid(row=2, column=0, sticky="w")
        self.strategy_combo = ttk.Combobox(
            settings_card,
            textvariable=self.strategy_name,
            values=list(INPUT_STRATEGIES),
            state="readonly",
            width=38,
        )
        self.strategy_combo.grid(row=2, column=1, columnspan=2, sticky="w", padx=(10, 0))
        self.strategy_combo.bind("<<ComboboxSelected>>", self.apply_strategy)

        for row, label, choices, variable, attribute in (
            (3, "空格方式", SPACE_MODES, self.space_mode, "space_combo"),
            (4, "Tab 方式", TAB_MODES, self.tab_mode, "tab_combo"),
            (5, "换行方式", NEWLINE_MODES, self.newline_mode, "newline_combo"),
        ):
            ttk.Label(settings_card, text=label, style="Card.TLabel").grid(
                row=row, column=0, sticky="w", pady=(6, 0)
            )
            combo = ttk.Combobox(
                settings_card,
                textvariable=variable,
                values=list(choices),
                state="readonly",
                width=38,
            )
            combo.grid(row=row, column=1, columnspan=2, sticky="w", padx=(10, 0), pady=(6, 0))
            setattr(self, attribute, combo)

        self.literal_check = ttk.Checkbutton(
            settings_card,
            text="兼容旧逐键模式（覆盖以上选择：Unicode 空格/Tab + 真实 Enter）",
            variable=self.literal_mode,
            style="Card.TCheckbutton",
            command=self.update_strategy_controls,
        )
        self.literal_check.grid(row=6, column=0, columnspan=4, sticky="w", pady=(6, 4))
        ttk.Label(
            settings_card,
            text="逐键模式不写剪贴板、不发送 Ctrl+V、不调用 SetValue，也不因目标不可读而停止；整段策略失败后不会回退逐键。",
            style="CardMuted.TLabel",
            wraplength=900,
        ).grid(row=7, column=0, columnspan=4, sticky="w", pady=(0, 10))

        ttk.Label(settings_card, text="开始前等待", style="Card.TLabel").grid(row=8, column=0, sticky="w")
        self.countdown_spin = ttk.Spinbox(
            settings_card,
            from_=0,
            to=60,
            increment=1,
            width=6,
            textvariable=self.countdown_text,
            justify="center",
        )
        self.countdown_spin.grid(row=8, column=1, sticky="w", padx=(10, 0))
        ttk.Label(settings_card, text="秒", style="CardMuted.TLabel").grid(row=8, column=2, sticky="w", padx=(6, 24))

        ttk.Label(settings_card, text="逐键字符间隔", style="Card.TLabel").grid(row=9, column=0, sticky="w", pady=(8, 0))
        self.interval_spin = ttk.Spinbox(
            settings_card,
            from_=0,
            to=1000,
            increment=5,
            width=6,
            textvariable=self.interval_text,
            justify="center",
        )
        self.interval_spin.grid(row=9, column=1, sticky="w", padx=(10, 0), pady=(8, 0))
        ttk.Label(settings_card, text="毫秒", style="CardMuted.TLabel").grid(
            row=9, column=2, sticky="w", padx=(6, 24), pady=(8, 0)
        )
        ttk.Label(
            settings_card,
            text="提示：间隔过短（尤其长代码段）容易出现字符缺失、符号丢失或换行错误；可调至 30ms 或更慢后重试。",
            style="CardMuted.TLabel",
            wraplength=900,
        ).grid(row=10, column=0, columnspan=3, sticky="w", pady=(6, 0))

        ttk.Label(settings_card, text="粘贴后等待", style="Card.TLabel").grid(row=11, column=0, sticky="w", pady=(8, 0))
        self.paste_settle_spin = ttk.Spinbox(
            settings_card,
            from_=0,
            to=5000,
            increment=100,
            width=6,
            textvariable=self.paste_settle_text,
            justify="center",
        )
        self.paste_settle_spin.grid(row=11, column=1, sticky="w", padx=(10, 0), pady=(8, 0))
        ttk.Label(settings_card, text="毫秒", style="CardMuted.TLabel").grid(
            row=11, column=2, sticky="w", padx=(6, 24), pady=(8, 0)
        )

        self.run_button = ttk.Button(
            settings_card,
            text="运行输入",
            style="Accent.TButton",
            command=self.start_typing,
        )
        self.run_button.grid(row=8, column=3, rowspan=4, sticky="e", padx=(16, 0))

        preview_card = ttk.Frame(body, style="Card.TFrame", padding=16)
        preview_card.grid(row=2, column=0, sticky="nsew")
        preview_card.columnconfigure(0, weight=1)
        preview_header = ttk.Frame(preview_card, style="Card.TFrame")
        preview_header.grid(row=0, column=0, sticky="ew")
        preview_header.columnconfigure(0, weight=1)
        ttk.Label(preview_header, text="空白与缩进检查", style="Section.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(
            preview_header,
            text="空格 ·    Tab →    换行 ↵",
            style="CardMuted.TLabel",
        ).grid(row=0, column=1, sticky="e")

        preview_frame = ttk.Frame(preview_card, style="Card.TFrame")
        preview_frame.grid(row=1, column=0, sticky="nsew", pady=(8, 0))
        preview_frame.columnconfigure(0, weight=1)
        preview_frame.rowconfigure(0, weight=1)
        self.preview = tk.Text(
            preview_frame,
            height=5,
            wrap="none",
            font=("Consolas", 10),
            bg="#F8FAFC",
            fg="#334155",
            relief="flat",
            highlightthickness=1,
            highlightbackground=self.BORDER,
            state="disabled",
            padx=10,
            pady=8,
        )
        self.preview.grid(row=0, column=0, sticky="nsew")
        preview_vbar = ttk.Scrollbar(preview_frame, orient="vertical", command=self.preview.yview)
        preview_vbar.grid(row=0, column=1, sticky="ns")
        preview_hbar = ttk.Scrollbar(preview_frame, orient="horizontal", command=self.preview.xview)
        preview_hbar.grid(row=1, column=0, sticky="ew")
        self.preview.configure(yscrollcommand=preview_vbar.set, xscrollcommand=preview_hbar.set)
        preview_card.rowconfigure(1, weight=1)
        preview_card.rowconfigure(3, weight=1)

        ttk.Label(preview_card, text="逐行格式分析", style="Section.TLabel").grid(
            row=2, column=0, sticky="w", pady=(10, 4)
        )
        details_frame = ttk.Frame(preview_card, style="Card.TFrame")
        details_frame.grid(row=3, column=0, sticky="nsew")
        details_frame.columnconfigure(0, weight=1)
        details_frame.rowconfigure(0, weight=1)
        self.line_details = tk.Text(
            details_frame,
            height=3,
            wrap="none",
            font=("Consolas", 9),
            bg="#F8FAFC",
            fg="#334155",
            relief="flat",
            highlightthickness=1,
            highlightbackground=self.BORDER,
            state="disabled",
            padx=10,
            pady=6,
        )
        self.line_details.grid(row=0, column=0, sticky="nsew")
        details_vbar = ttk.Scrollbar(details_frame, orient="vertical", command=self.line_details.yview)
        details_vbar.grid(row=0, column=1, sticky="ns")
        details_hbar = ttk.Scrollbar(details_frame, orient="horizontal", command=self.line_details.xview)
        details_hbar.grid(row=1, column=0, sticky="ew")
        self.line_details.configure(yscrollcommand=details_vbar.set, xscrollcommand=details_hbar.set)

        ttk.Label(preview_card, text="实际输入路径与结果验证", style="Section.TLabel").grid(
            row=4, column=0, sticky="w", pady=(10, 4)
        )
        debug_frame = ttk.Frame(preview_card, style="Card.TFrame")
        debug_frame.grid(row=5, column=0, sticky="nsew")
        debug_frame.columnconfigure(0, weight=1)
        debug_frame.rowconfigure(0, weight=1)
        self.debug_view = tk.Text(
            debug_frame,
            height=4,
            wrap="none",
            font=("Consolas", 9),
            bg="#F8FAFC",
            fg="#334155",
            relief="flat",
            highlightthickness=1,
            highlightbackground=self.BORDER,
            state="disabled",
            padx=10,
            pady=6,
        )
        self.debug_view.grid(row=0, column=0, sticky="nsew")
        debug_vbar = ttk.Scrollbar(debug_frame, orient="vertical", command=self.debug_view.yview)
        debug_vbar.grid(row=0, column=1, sticky="ns")
        debug_hbar = ttk.Scrollbar(debug_frame, orient="horizontal", command=self.debug_view.xview)
        debug_hbar.grid(row=1, column=0, sticky="ew")
        self.debug_view.configure(yscrollcommand=debug_vbar.set, xscrollcommand=debug_hbar.set)
        preview_card.rowconfigure(5, weight=1)
        self.set_debug_text(
            "尚未运行。将显示逐字符事件、实际输入方式、前台焦点、目标读回和最终状态；SendInput 接受事件不等于目标文本一致。"
        )

        status_frame = ttk.Frame(body, style="TFrame")
        status_frame.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        status_frame.columnconfigure(0, weight=1)
        ttk.Label(status_frame, textvariable=self.status_text, style="Muted.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(status_frame, text="F8 全局停止", style="Muted.TLabel").grid(row=0, column=1, sticky="e")
        self.progress = ttk.Progressbar(
            status_frame,
            style="Horizontal.TProgressbar",
            maximum=1,
            value=0,
        )
        self.progress.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))

    def select_all(self, _event=None) -> str:
        self.editor.tag_add("sel", "1.0", "end-1c")
        return "break"

    def current_text(self) -> str:
        return self.editor.get("1.0", "end-1c")

    def set_debug_text(self, text: str) -> None:
        self.debug_view.configure(state="normal")
        self.debug_view.delete("1.0", "end")
        self.debug_view.insert("1.0", text)
        self.debug_view.configure(state="disabled")

    def update_content_analysis(self) -> None:
        if self.worker is not None and self.worker.is_alive():
            return
        analysis = analyze_text(self.current_text())
        self.stats_text.set(
            f"共 {analysis['total']} 字符    {analysis['line_count']} 行    "
            f"英文 {analysis['english']}    中文 {analysis['chinese']}    "
            f"空格 {analysis['spaces']}    Tab {analysis['tabs']}    换行 {analysis['newlines']}    "
            f"空行 {analysis['blank_line_count']}    末尾换行 {'是' if analysis['ends_with_newline'] else '否'}    "
            f"快照 {analysis['sha256'][:12]}"
        )
        self.preview.configure(state="normal")
        self.preview.delete("1.0", "end")
        self.preview.insert("1.0", visible_whitespace(analysis["normalized_text"]))
        self.preview.configure(state="disabled")
        self.line_details.configure(state="normal")
        self.line_details.delete("1.0", "end")
        self.line_details.insert("1.0", format_line_analysis(analysis))
        self.line_details.configure(state="disabled")

    def read_clipboard(self) -> None:
        try:
            text = self.root.clipboard_get()
        except tk.TclError:
            messagebox.showinfo("无法读取", "系统剪切板中没有可读取的文本。", parent=self.root)
            return
        self.editor.delete("1.0", "end")
        self.editor.insert("1.0", text)
        self.update_content_analysis()
        self.status_text.set("已读取系统剪切板；仅统一换行格式，其他字符保持原样。")

    def apply_scene(self, _event=None) -> None:
        scene = SCENES[self.scene_name.get()]
        self.countdown_text.set(str(scene.countdown))
        self.interval_text.set(str(scene.interval))
        self.update_strategy_controls()

    def apply_strategy(self, _event=None) -> None:
        self.update_strategy_controls()

    def update_strategy_controls(self) -> None:
        busy = self.worker is not None and self.worker.is_alive()
        keyboard = INPUT_STRATEGIES[self.strategy_name.get()] == INPUT_STRATEGY_KEYBOARD
        independent = keyboard and not busy and not self.literal_mode.get()
        self.literal_check.configure(state="normal" if keyboard and not busy else "disabled")
        for combo in (self.space_combo, self.tab_combo, self.newline_combo):
            combo.configure(state="readonly" if independent else "disabled")
        self.interval_spin.configure(state="normal" if keyboard and not busy else "disabled")
        self.paste_settle_spin.configure(state="normal" if not keyboard and not busy else "disabled")
        hint = SCENES[self.scene_name.get()].hint
        if keyboard:
            hint += " 多行 Enter 可能触发目标自动缩进；发送成功不代表最终文本一致。"
        else:
            hint += " 整段模式失败后不会回退逐键；粘贴或直写提交后须核对目标。"
        self.scene_hint.set(hint)

    def set_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        self.read_button.configure(state=state)
        self.scene_combo.configure(state="disabled" if busy else "readonly")
        self.strategy_combo.configure(state="disabled" if busy else "readonly")
        self.literal_check.configure(state=state)
        self.countdown_spin.configure(state=state)
        for combo in (self.space_combo, self.tab_combo, self.newline_combo):
            combo.configure(state="disabled" if busy else "readonly")
        self.interval_spin.configure(state="disabled" if busy else "normal")
        self.paste_settle_spin.configure(state="disabled" if busy else "normal")
        self.editor.configure(state=state)
        if busy:
            self.run_button.configure(text="停止输入", style="Danger.TButton", command=self.request_stop)
        else:
            self.run_button.configure(text="运行输入", style="Accent.TButton", command=self.start_typing)
            self.update_strategy_controls()

    def start_typing(self) -> None:
        if self.worker is not None and self.worker.is_alive():
            messagebox.showinfo("正在运行", "已有输入任务正在执行，请先停止或等待完成。", parent=self.root)
            return
        raw_text = self.current_text()
        analysis = analyze_text(raw_text)
        text = analysis["normalized_text"]
        if not text:
            messagebox.showwarning("无法运行", "请先粘贴或输入要发送的内容。", parent=self.root)
            return
        if "\x00" in text:
            messagebox.showwarning("无法运行", "内容包含无法输入的空字符，请移除后再运行。", parent=self.root)
            return

        try:
            countdown = int(self.countdown_text.get())
            interval = int(self.interval_text.get())
            paste_settle_ms = int(self.paste_settle_text.get())
        except ValueError:
            messagebox.showwarning("参数无效", "等待时间、字符间隔和粘贴后等待必须填写整数。", parent=self.root)
            return
        if not 0 <= countdown <= 60 or not 0 <= interval <= 1000:
            messagebox.showwarning("参数无效", "等待时间需为 0–60 秒，字符间隔需为 0–1000 毫秒。", parent=self.root)
            return
        if not 0 <= paste_settle_ms <= 5000:
            messagebox.showwarning("参数无效", "粘贴后等待需为 0–5000 毫秒。", parent=self.root)
            return
        if not IS_WINDOWS:
            messagebox.showerror("不支持的系统", "此工具目前仅支持 Windows。", parent=self.root)
            return

        self.total_chars = analysis["total"]
        self.progress.configure(maximum=self.total_chars, value=0)
        self.set_debug_text(
            f"原始快照已保存：SHA-256 {analysis['sha256']}；"
            f"解析重建 {'一致' if parse_text(raw_text).reconstruct() == text else '不一致'}；"
            f"空格 {analysis['spaces']}，Tab {analysis['tabs']}，换行 {analysis['newlines']}，"
            f"末尾换行 {'是' if analysis['ends_with_newline'] else '否'}。\n"
            "逐键模式将在倒计时后直接发送；整段模式才会先捕获目标控件。任何路径都不会隐式切换输入方式。"
        )
        self.stop_event = threading.Event()
        self.events = queue.Queue()
        self.worker = threading.Thread(
            target=input_worker,
            args=(
                text,
                countdown,
                interval,
                self.stop_event,
                self.events,
                True if self.literal_mode.get() else None,
            ),
            kwargs={
                "strategy": INPUT_STRATEGIES[self.strategy_name.get()],
                "paste_settle_seconds": paste_settle_ms / 1000,
                "verification_timeout": PASTE_VERIFY_TIMEOUT_SECONDS,
                "space_mode": SPACE_MODES[self.space_mode.get()],
                "tab_mode": TAB_MODES[self.tab_mode.get()],
                "newline_mode": NEWLINE_MODES[self.newline_mode.get()],
            },
            name="auto-typer",
            daemon=True,
        )
        self.set_busy(True)
        self.status_text.set("正在准备输入…")
        self.worker.start()
        self.root.after(40, self.process_events)

    def request_stop(self) -> None:
        if self.stop_event is None or self.stop_event.is_set():
            return
        self.stop_event.set()
        self.run_button.configure(text="正在停止…", state="disabled")
        self.status_text.set("正在停止输入…")

    def finish_run(self, event: dict) -> None:
        self.worker = None
        self.set_busy(False)
        kind = event["type"]
        count = event.get("count", 0)
        verification_text = format_verification_data(event.get("verification"))
        state_text = format_state_data(event.get("state"))
        self.progress.configure(value=count)
        if kind == "done":
            self.progress.configure(value=self.total_chars)
            status = (event.get("verification") or {}).get("status", "unverified")
            if status == "verified":
                self.status_text.set(f"输入完成：{count} 字符快照已提交，目标文本读回一致。")
            elif status == "mismatch":
                self.status_text.set("输入已提交，但目标文本校验不一致；请查看诊断，勿视为成功。")
            else:
                self.status_text.set(f"已提交 {count} 字符快照；目标最终文本无法验证。")
        elif kind == "stopped":
            self.status_text.set(f"已停止，已处理 {count} / {self.total_chars} 个字符。")
        elif kind == "error":
            message = f"已处理 {count} 个字符。{event['message']}"
            self.status_text.set("输入失败，请查看错误信息。")
            messagebox.showerror(
                "输入失败",
                message
                + "\n\n如果目标程序以管理员权限运行，请尝试以管理员身份运行本工具。",
                parent=self.root,
            )
        details = [state_text, verification_text]
        if event.get("source"):
            details.append(f"原始快照统计：{event['source']}")
        if event.get("log"):
            details.extend(event["log"])
        self.set_debug_text("\n".join(details))

    def process_events(self) -> None:
        if self.events is None:
            return
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            kind = event["type"]
            if kind == "countdown":
                self.status_text.set(
                    f"请在 {event['remaining']} 秒内切换到目标窗口并定位光标；按 F8 可停止。"
                )
            elif kind == "typing":
                strategy = INPUT_STRATEGIES[self.strategy_name.get()]
                self.status_text.set(
                    "正在逐字符模拟键盘输入… 按 F8 可停止。"
                    if strategy == INPUT_STRATEGY_KEYBOARD
                    else "正在捕获目标控件并选择整段输入路径… 按 F8 可停止。"
                )
            elif kind == "target":
                target = event["target"]
                phase = "输入后只读检测" if event.get("phase") == "after_input" else "输入前目标能力"
                capability = "支持安全整值写入" if target.get("direct_writable") else "不支持安全整值写入"
                self.set_debug_text(
                    f"{phase}：{capability}\n{target.get('reason', '')}\n"
                    f"文本可读 {'是' if target.get('readable') else '否'}；"
                    f"ValuePattern {'是' if target.get('value_pattern_supported') else '否'}；"
                    f"选区 {target.get('start')}–{target.get('end')}"
                )
            elif kind == "diagnostic":
                details = [event.get("text", "输入路径诊断")]
                if event.get("log"):
                    details.extend(event["log"])
                self.set_debug_text("\n".join(details))
            elif kind == "progress":
                count = event["count"]
                percent = int(count * 100 / self.total_chars) if self.total_chars else 100
                self.progress.configure(value=count)
                state = event.get("state") or {}
                if state.get("method") in ("uia_value", "clipboard"):
                    self.status_text.set("完整文本块已提交，正在等待目标处理并读回… 按 F8 可停止。")
                else:
                    self.status_text.set(
                        f"正在处理 {count} / {self.total_chars}（{percent}%）· 按 F8 可停止。"
                    )
                if state:
                    details = format_state_data(state)
                    character = event.get("character")
                    if character:
                        details += "\n" + format_character_event_data(character)
                    self.set_debug_text(details)
            else:
                self.finish_run(event)
                self.events = None
                return
        self.root.after(40, self.process_events)

    def on_close(self) -> None:
        if self.stop_event is not None:
            self.stop_event.set()
        self.root.destroy()


def main() -> None:
    if tk is None:
        raise RuntimeError("当前 Python 未安装 Tkinter，无法启动图形界面。")
    root = tk.Tk()
    AutoTyperApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
