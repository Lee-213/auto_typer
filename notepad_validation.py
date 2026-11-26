from __future__ import annotations

import argparse
import codecs
import ctypes
import hashlib
import json
import platform
import queue
import shutil
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import auto_typer
from target_text import TargetUnavailable
from text_parser import compare_text, normalize_newlines, parse_text


PYTHON_SAMPLE = (
    "def main():\n"
    '    print("第一行")\n'
    '        print("第三行")\n'
    "\n"
    "value = 100\n"
    "\n"
    "if value > 0:\n"
    '    print("测试缩进")\n'
    "\n"
    'print("结束")'
)


@dataclass(frozen=True)
class ValidationCase:
    case_id: str
    description: str
    text: str


CASES = (
    ValidationCase("python_sample", "指定 Python 样例，第三行故意保留 8 个空格", PYTHON_SAMPLE),
    ValidationCase("python_sample_final_lf", "指定 Python 样例，末尾有换行", PYTHON_SAMPLE + "\n"),
    ValidationCase("single_space", "一个连续空格", "A B"),
    ValidationCase("four_spaces", "四个连续空格", "A    B"),
    ValidationCase("eight_spaces", "八个连续空格", "A        B"),
    ValidationCase("twelve_spaces", "十二个连续空格", "A            B"),
    ValidationCase("indent_widths", "0/1/2/4/8/12 个行首空格", "\n".join(" " * width + f"L{width}" for width in (0, 1, 2, 4, 8, 12))),
    ValidationCase("ten_four_space_lines", "连续十行四个行首空格", "\n".join("    value = 1" for _ in range(10))),
    ValidationCase("multiple_tabs", "三个连续 Tab", "A\t\t\tB"),
    ValidationCase("mixed_whitespace", "空格/Tab 混合顺序和纯空白行", " \t  \tA \t B  \n\t \t\n"),
    ValidationCase("blank_lines", "两个以上连续空行", "A\n\n\nB"),
    ValidationCase("trailing_spaces", "行尾空格和末行行尾空格", "A  \nB   "),
    ValidationCase("trailing_tabs", "行尾 Tab 和末行行尾 Tab", "A\t\t\nB\t"),
    ValidationCase("unicode", "中文、英文、数字、标点和 Emoji", "中文 English 123 !? 😀\n第二行"),
    ValidationCase("final_lf", "末尾换行存在", "A\nB\n"),
    ValidationCase("no_final_lf", "末尾换行不存在", "A\nB"),
    ValidationCase("newline_normalization", "CRLF/CR 仅规范化为 LF", "A\r\nB\rC\n"),
)

VK_S = 0x53
WM_CLOSE = 0x0010


def forbidden_call(name: str):
    def forbidden(*_args, **_kwargs):
        raise AssertionError(f"键盘独立路径禁止调用 {name}")

    return forbidden


@contextmanager
def keyboard_only_guards():
    names = (
        "capture_clipboard_text",
        "write_clipboard_text",
        "read_clipboard_text",
        "restore_clipboard_text",
        "send_paste_shortcut",
        "TargetController",
    )
    originals = {name: getattr(auto_typer, name) for name in names}
    try:
        for name in names:
            setattr(auto_typer, name, forbidden_call(name))
        yield
    finally:
        for name, original in originals.items():
            setattr(auto_typer, name, original)


def configure_user32() -> dict:
    auto_typer._require_windows_input()
    user32 = auto_typer.user32
    user32.IsWindowVisible.argtypes = [auto_typer.wintypes.HWND]
    user32.IsWindowVisible.restype = auto_typer.wintypes.BOOL
    user32.GetClassNameW.argtypes = [auto_typer.wintypes.HWND, auto_typer.wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [auto_typer.wintypes.HWND, auto_typer.wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.SetForegroundWindow.argtypes = [auto_typer.wintypes.HWND]
    user32.SetForegroundWindow.restype = auto_typer.wintypes.BOOL
    user32.PostMessageW.argtypes = [auto_typer.wintypes.HWND, auto_typer.wintypes.UINT, auto_typer.wintypes.WPARAM, auto_typer.wintypes.LPARAM]
    user32.PostMessageW.restype = auto_typer.wintypes.BOOL

    pointer_size = ctypes.sizeof(ctypes.c_void_p)
    expected = {
        "INPUT": 40 if pointer_size == 8 else 28,
        "INPUT_UNION": 32 if pointer_size == 8 else 24,
        "KEYBDINPUT": 24 if pointer_size == 8 else 16,
    }
    actual = {
        "INPUT": ctypes.sizeof(auto_typer.INPUT),
        "INPUT_UNION": ctypes.sizeof(auto_typer.INPUT_UNION),
        "KEYBDINPUT": ctypes.sizeof(auto_typer.KEYBDINPUT),
    }
    if actual != expected:
        raise RuntimeError(f"Windows INPUT ABI 不匹配：实际 {actual}，预期 {expected}")
    return {
        "pointer_size": pointer_size,
        "structure_sizes": actual,
        "input_data_offset": auto_typer.INPUT.data.offset,
        "keybdinput_extra_info_offset": auto_typer.KEYBDINPUT.dwExtraInfo.offset,
        "windows_api_error": auto_typer.WINDOWS_API_ERROR,
    }


def find_notepad_window(token: str) -> int | None:
    user32 = auto_typer.user32
    callback_type = ctypes.WINFUNCTYPE(
        auto_typer.wintypes.BOOL,
        auto_typer.wintypes.HWND,
        auto_typer.wintypes.LPARAM,
    )
    matches: list[int] = []

    def callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        class_name = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, class_name, len(class_name))
        if class_name.value != "Notepad":
            return True
        title = ctypes.create_unicode_buffer(1024)
        user32.GetWindowTextW(hwnd, title, len(title))
        if token in title.value:
            matches.append(int(hwnd))
        return True

    user32.EnumWindows.argtypes = [callback_type, auto_typer.wintypes.LPARAM]
    user32.EnumWindows.restype = auto_typer.wintypes.BOOL
    user32.EnumWindows(callback_type(callback), 0)
    if len(matches) > 1:
        raise RuntimeError(f"发现多个匹配记事本窗口：{matches}")
    return matches[0] if matches else None


def wait_for_notepad_window(token: str, timeout: float = 12.0) -> int:
    deadline = time.monotonic() + timeout
    while True:
        hwnd = find_notepad_window(token)
        if hwnd is not None:
            return hwnd
        if time.monotonic() >= deadline:
            raise RuntimeError(f"未找到标题包含 {token!r} 的可见记事本窗口")
        time.sleep(0.1)


def activate_window(hwnd: int) -> None:
    user32 = auto_typer.user32
    user32.SetForegroundWindow(hwnd)
    deadline = time.monotonic() + 5.0
    while True:
        if int(user32.GetForegroundWindow()) == hwnd:
            return
        if time.monotonic() >= deadline:
            raise RuntimeError("无法把隔离记事本窗口切换到前台，已停止发送输入")
        time.sleep(0.05)


def window_process_id(hwnd: int) -> int:
    process_id = auto_typer.wintypes.DWORD()
    auto_typer.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
    return int(process_id.value)


def pressed_modifiers() -> list[str]:
    keys = (
        (0x10, "Shift"),
        (0x11, "Control"),
        (0x12, "Alt"),
        (0x5B, "Left Windows"),
        (0x5C, "Right Windows"),
    )
    return [
        name
        for virtual_key, name in keys
        if auto_typer.user32.GetAsyncKeyState(virtual_key) & 0x8000
    ]


def send_control_shortcut(virtual_key: int) -> dict:
    events = (
        auto_typer._keyboard_event(auto_typer.VK_CONTROL, 0, auto_typer.KEYEVENTF_KEYUP),
        auto_typer._keyboard_event(virtual_key, 0, auto_typer.KEYEVENTF_KEYUP),
        auto_typer._keyboard_event(auto_typer.VK_CONTROL, 0, 0),
        auto_typer._keyboard_event(virtual_key, 0, 0),
        auto_typer._keyboard_event(virtual_key, 0, auto_typer.KEYEVENTF_KEYUP),
        auto_typer._keyboard_event(auto_typer.VK_CONTROL, 0, auto_typer.KEYEVENTF_KEYUP),
    )
    accepted = auto_typer._send_input(events)
    return {"requested": len(events), "accepted": accepted}


def wait_for_saved_utf8(path: Path, hwnd: int, timeout: float = 6.0) -> bytes:
    deadline = time.monotonic() + timeout
    previous: bytes | None = None
    stable_reads = 0
    while True:
        if int(auto_typer.user32.GetForegroundWindow()) != hwnd:
            raise RuntimeError("保存期间前台窗口发生变化，可能弹出对话框；未继续发送按键")
        raw = path.read_bytes()
        if len(raw) > len(codecs.BOM_UTF8):
            try:
                raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                raw = b""
            else:
                stable_reads = stable_reads + 1 if raw == previous else 0
                if stable_reads >= 2:
                    return raw
        previous = raw
        if time.monotonic() >= deadline:
            raise RuntimeError("等待记事本保存 UTF-8 文件超时")
        time.sleep(0.1)


def close_saved_window(hwnd: int, timeout: float = 4.0) -> bool:
    user32 = auto_typer.user32
    if not user32.PostMessageW(hwnd, WM_CLOSE, 0, 0):
        return False
    deadline = time.monotonic() + timeout
    while True:
        if not user32.IsWindowVisible(hwnd):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def line_results(expected: str, actual: str) -> list[dict]:
    expected_lines = parse_text(expected).lines
    actual_lines = parse_text(actual).lines
    results = []
    for index in range(max(len(expected_lines), len(actual_lines))):
        expected_line = expected_lines[index].as_dict() if index < len(expected_lines) else None
        actual_line = actual_lines[index].as_dict() if index < len(actual_lines) else None
        results.append(
            {
                "line_number": index + 1,
                "expected": expected_line,
                "actual": actual_line,
                "match": expected_line == actual_line,
            }
        )
    return results


def expected_methods(
    text: str,
    space_mode: str,
    tab_mode: str,
    newline_mode: str,
) -> list[str]:
    methods = []
    for char in text:
        if char == " ":
            methods.append("vk_space" if space_mode == auto_typer.SPACE_MODE_VIRTUAL else "unicode")
        elif char == "\t":
            methods.append("vk_tab" if tab_mode == auto_typer.TAB_MODE_VIRTUAL else "unicode_tab")
        elif char == "\n":
            methods.append("vk_return" if newline_mode == auto_typer.NEWLINE_MODE_ENTER else "unicode_lf")
        else:
            methods.append("unicode")
    return methods


def drain_events(events: queue.Queue) -> list[dict]:
    received = []
    while not events.empty():
        received.append(events.get_nowait())
    return received


def run_case(
    case: ValidationCase,
    output_dir: Path,
    interval_ms: int,
    space_mode: str,
    tab_mode: str,
    newline_mode: str,
) -> dict:
    expected = normalize_newlines(case.text)
    token = f"v23_{uuid.uuid4().hex[:8]}_{case.case_id}"
    path = output_dir / f"{token}.txt"
    path.write_bytes(codecs.BOM_UTF8)
    result = {
        "case_id": case.case_id,
        "description": case.description,
        "source_text": expected,
        "source_repr": repr(expected),
        "source_sha256": parse_text(expected).sha256,
        "source_total": len(expected),
        "modes": {
            "space_mode": space_mode,
            "tab_mode": tab_mode,
            "newline_mode": newline_mode,
        },
        "file": str(path),
        "status": "error",
        "fatal": False,
    }
    process = None
    hwnd = None
    saved = False
    factory_calls = 0
    try:
        notepad = shutil.which("notepad.exe")
        if not notepad:
            raise RuntimeError("找不到 notepad.exe")
        result["notepad_executable"] = notepad
        process = subprocess.Popen(
            [notepad, str(path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        hwnd = wait_for_notepad_window(token)
        activate_window(hwnd)
        modifiers = pressed_modifiers()
        result["pressed_modifiers_before"] = modifiers
        if modifiers or auto_typer.is_f8_pressed():
            raise RuntimeError("检测到修饰键或 F8 按下，已取消真实输入")
        result["window"] = {
            "hwnd": hwnd,
            "process_id": window_process_id(hwnd),
            "foreground_before": auto_typer.get_foreground_window_info(),
            "file_token": token,
        }

        events = queue.Queue()

        def unavailable_target():
            nonlocal factory_calls
            factory_calls += 1
            raise TargetUnavailable("真实记事本测试禁用 UIA 基线捕获，改用保存文件验证")

        with keyboard_only_guards():
            auto_typer.input_worker(
                case.text,
                0,
                interval_ms,
                threading.Event(),
                events,
                target_factory=unavailable_target,
                space_mode=space_mode,
                tab_mode=tab_mode,
                newline_mode=newline_mode,
            )
        received = drain_events(events)
        result["worker_events"] = received
        terminal = received[-1]
        result["worker_terminal"] = terminal
        diagnostics = terminal["diagnostics"]
        character_events = terminal["character_events"]
        expected_event_count = sum(2 * len(auto_typer.utf16_units(char)) for char in expected)
        methods = expected_methods(expected, space_mode, tab_mode, newline_mode)
        checks = {
            "worker_done": terminal["type"] == "done",
            "completed_count": terminal["count"] == len(expected),
            "keyboard_method": diagnostics["method"] == "keyboard",
            "keyboard_executed": diagnostics["keyboard_input"] is True,
            "clipboard_not_used": diagnostics["clipboard_write"] == "not_used",
            "paste_not_used": diagnostics["paste_shortcut"] == "not_used",
            "post_input_capture_only": diagnostics["target_capture_phase"] == "after_input",
            "target_factory_after_input": factory_calls == 1,
            "sendinput_requested": diagnostics["sendinput_requested"] == expected_event_count,
            "sendinput_accepted": diagnostics["sendinput_accepted"] == expected_event_count,
            "character_event_count": terminal["character_event_count"] == len(expected),
            "character_methods": [event["method"] for event in character_events] == methods,
            "character_success": all(
                event["status"] == "sent"
                and event["accepted"] == event["requested"]
                and event["key_down_success"] is True
                and event["key_up_success"] is True
                for event in character_events
            ),
            "focus_preserved": diagnostics["focus_preserved"] is True,
            "worker_honest_unverified": terminal["verification"]["status"] == "unverified",
            "first_events": [event["type"] for event in received[:2]] == ["typing", "diagnostic"],
            "target_event_after_input": all(
                event.get("phase") == "after_input"
                for event in received
                if event["type"] == "target"
            ),
        }
        result["path_checks"] = checks
        if not all(checks.values()):
            failed = [name for name, passed in checks.items() if not passed]
            raise RuntimeError(f"生产键盘路径检查失败：{', '.join(failed)}")

        time.sleep(0.2)
        if int(auto_typer.user32.GetForegroundWindow()) != hwnd:
            raise RuntimeError("输入后记事本失去前台焦点，未发送保存快捷键")
        result["save_shortcut"] = send_control_shortcut(VK_S)
        if result["save_shortcut"]["accepted"] != 6:
            raise RuntimeError("保存快捷键未被系统完整接受")
        raw = wait_for_saved_utf8(path, hwnd)
        saved = True
        actual_with_bom = raw.decode("utf-8-sig")
        actual = normalize_newlines(actual_with_bom)
        comparison = compare_text(expected, actual)
        lines = line_results(expected, actual)
        result.update(
            {
                "actual_text": actual,
                "actual_repr": repr(actual),
                "actual_raw_sha256": hashlib.sha256(raw).hexdigest(),
                "utf8_bom": raw.startswith(codecs.BOM_UTF8),
                "saved_file_status": comparison["status"],
                "exact_match": comparison["exact_match"],
                "comparison": comparison,
                "line_results": lines,
                "all_lines_match": all(line["match"] for line in lines),
                "foreground_after_save": auto_typer.get_foreground_window_info(),
            }
        )
        result["status"] = (
            "pass"
            if comparison["exact_match"] and all(line["match"] for line in lines)
            else "fail"
        )
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        if hwnd is None:
            result["fatal"] = True
        if hwnd is not None and not saved:
            try:
                if int(auto_typer.user32.GetForegroundWindow()) == hwnd:
                    result["recovery_save_shortcut"] = send_control_shortcut(VK_S)
                    raw = wait_for_saved_utf8(path, hwnd)
                    saved = True
                    actual = normalize_newlines(raw.decode("utf-8-sig"))
                    result["recovery_actual_repr"] = repr(actual)
            except Exception as recovery_exc:
                result["recovery_error"] = f"{type(recovery_exc).__name__}: {recovery_exc}"
    finally:
        result["window_closed"] = close_saved_window(hwnd) if hwnd is not None and saved else False
        result["cleanup_note"] = (
            "已保存并关闭隔离记事本窗口"
            if result["window_closed"]
            else "为避免丢弃未保存内容，保留记事本窗口；请手动检查临时文件"
        )
        if process is not None and process.poll() is None:
            result["launcher_process_still_running"] = True
    return result


def write_results(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="使用真实 SendInput 在隔离记事本中验证 V2.3 文本保真")
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).resolve().parent / "validation_results")
    parser.add_argument("--interval-ms", type=int, default=20)
    parser.add_argument("--space-mode", choices=(auto_typer.SPACE_MODE_UNICODE, auto_typer.SPACE_MODE_VIRTUAL), default=auto_typer.SPACE_MODE_VIRTUAL)
    parser.add_argument("--tab-mode", choices=(auto_typer.TAB_MODE_VIRTUAL, auto_typer.TAB_MODE_UNICODE), default=auto_typer.TAB_MODE_VIRTUAL)
    parser.add_argument("--newline-mode", choices=(auto_typer.NEWLINE_MODE_ENTER, auto_typer.NEWLINE_MODE_UNICODE_LF), default=auto_typer.NEWLINE_MODE_ENTER)
    parser.add_argument("--case", action="append", choices=[case.case_id for case in CASES])
    parser.add_argument("--list-cases", action="store_true")
    args = parser.parse_args()
    if args.list_cases:
        for case in CASES:
            print(f"{case.case_id}: {case.description}")
        return 0
    if not auto_typer.IS_WINDOWS:
        print("真实记事本验证仅支持 Windows。", file=sys.stderr)
        return 2
    if args.interval_ms < 0:
        parser.error("--interval-ms 不能为负数")

    api_info = configure_user32()
    selected = [case for case in CASES if not args.case or case.case_id in args.case]
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
    output_dir = args.output_dir.resolve() / f"notepad_{run_id}"
    output_dir.mkdir(parents=True, exist_ok=False)
    results_path = output_dir / "results.json"
    payload = {
        "run_id": run_id,
        "started_at": datetime.now().astimezone().isoformat(),
        "platform": platform.platform(),
        "python": sys.version,
        "notepad_executable": shutil.which("notepad.exe"),
        "interval_ms": args.interval_ms,
        "modes": {
            "space_mode": args.space_mode,
            "tab_mode": args.tab_mode,
            "newline_mode": args.newline_mode,
        },
        "api": api_info,
        "input_path": "auto_typer.input_worker 默认 keyboard -> type_text -> SendInput",
        "clipboard_and_uia_guards": True,
        "results": [],
    }
    print(f"结果目录：{output_dir}")
    for case in selected:
        print(f"运行 {case.case_id} ...", flush=True)
        result = run_case(
            case,
            output_dir,
            args.interval_ms,
            args.space_mode,
            args.tab_mode,
            args.newline_mode,
        )
        payload["results"].append(result)
        payload["updated_at"] = datetime.now().astimezone().isoformat()
        write_results(results_path, payload)
        print(f"  {result['status']}: {result.get('saved_file_status', result.get('error', '未保存'))}", flush=True)
        if result["fatal"]:
            break

    passed = sum(result["status"] == "pass" for result in payload["results"])
    failed = sum(result["status"] == "fail" for result in payload["results"])
    errors = sum(result["status"] == "error" for result in payload["results"])
    payload["summary"] = {
        "selected": len(selected),
        "executed": len(payload["results"]),
        "passed": passed,
        "failed": failed,
        "errors": errors,
        "all_passed": passed == len(selected) and failed == 0 and errors == 0,
        "finished_at": datetime.now().astimezone().isoformat(),
    }
    write_results(results_path, payload)
    print(json.dumps(payload["summary"], ensure_ascii=False))
    print(f"完整证据：{results_path}")
    return 0 if payload["summary"]["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
