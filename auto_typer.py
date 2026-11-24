from __future__ import annotations

import ctypes
import math
import queue
import string
import sys
import threading
import time
import tkinter as tk
import unicodedata
from ctypes import wintypes
from dataclasses import dataclass
from tkinter import messagebox, ttk

IS_WINDOWS = sys.platform == "win32"

if IS_WINDOWS:
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

    INPUT_KEYBOARD = 1
    KEYEVENTF_KEYUP = 0x0002
    KEYEVENTF_UNICODE = 0x0004
    VK_TAB = 0x09
    VK_RETURN = 0x0D
    VK_SPACE = 0x20
    VK_F8 = 0x77

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SendInput.argtypes = [
        wintypes.UINT,
        ctypes.POINTER(INPUT),
        ctypes.c_int,
    ]
    user32.SendInput.restype = wintypes.UINT
    user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
    user32.GetAsyncKeyState.restype = ctypes.c_short


def normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


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
    return {
        "total": len(text),
        "english": sum(char in string.ascii_letters for char in text),
        "chinese": sum(is_chinese_char(char) for char in text),
        "spaces": text.count(" "),
        "tabs": text.count("\t"),
        "newlines": text.count("\n"),
    }


def visible_whitespace(text: str) -> str:
    return text.replace("\t", "→").replace(" ", "·").replace("\n", "↵\n")


def _keyboard_event(vk: int, scan: int, flags: int) -> INPUT:
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


def _send_input(events: tuple[INPUT, ...]) -> None:
    event_array = (INPUT * len(events))(*events)
    sent = user32.SendInput(len(events), event_array, ctypes.sizeof(INPUT))
    if sent != len(events):
        error_code = ctypes.get_last_error()
        if error_code:
            raise OSError(error_code, ctypes.FormatError(error_code))
        raise OSError("系统未接受模拟键盘事件")


def send_key(virtual_key: int) -> None:
    _send_input(
        (
            _keyboard_event(virtual_key, 0, 0),
            _keyboard_event(virtual_key, 0, KEYEVENTF_KEYUP),
        )
    )


def utf16_units(text: str) -> list[int]:
    data = text.encode("utf-16-le")
    return [data[index] | (data[index + 1] << 8) for index in range(0, len(data), 2)]


def send_unicode_character(char: str) -> None:
    for unit in utf16_units(char):
        _send_input(
            (
                _keyboard_event(0, unit, KEYEVENTF_UNICODE),
                _keyboard_event(0, unit, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP),
            )
        )


def send_character(char: str) -> None:
    if char == "\t":
        send_key(VK_TAB)
    elif char in ("\n", "\r"):
        send_key(VK_RETURN)
    elif char == " ":
        send_key(VK_SPACE)
    else:
        send_unicode_character(char)


def is_f8_pressed() -> bool:
    return bool(user32.GetAsyncKeyState(VK_F8) & 0x8000)


def type_text(
    text: str,
    interval_ms: int,
    stop_event: threading.Event | None = None,
    should_stop=None,
    on_progress=None,
) -> int:
    text = normalize_newlines(text)
    completed = 0
    interval = max(0, interval_ms) / 1000

    for index, char in enumerate(text):
        if (stop_event is not None and stop_event.is_set()) or (
            should_stop is not None and should_stop()
        ):
            break
        send_character(char)
        completed = index + 1
        if on_progress is not None:
            on_progress(completed)
        if interval and stop_event is not None:
            if stop_event.wait(interval):
                break
        elif interval:
            time.sleep(interval)

    return completed


def input_worker(
    text: str,
    countdown_seconds: int,
    interval_ms: int,
    stop_event: threading.Event,
    events: queue.Queue,
) -> None:
    completed = 0
    try:
        if not IS_WINDOWS:
            raise OSError("此工具目前仅支持 Windows。")

        deadline = time.monotonic() + countdown_seconds
        last_second = None
        while True:
            if stop_event.is_set() or is_f8_pressed():
                events.put({"type": "stopped", "count": completed})
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            second = math.ceil(remaining)
            if second != last_second:
                events.put({"type": "countdown", "remaining": second})
                last_second = second
            stop_event.wait(min(0.1, remaining))

        events.put({"type": "typing"})
        report_every = max(1, len(text) // 200)

        def report_progress(count: int) -> None:
            nonlocal completed
            completed = count
            if count % report_every == 0 or count == len(text):
                events.put({"type": "progress", "count": count})

        completed = type_text(
            text,
            interval_ms,
            stop_event,
            is_f8_pressed,
            report_progress,
        )
        if completed >= len(text):
            events.put({"type": "done", "count": completed})
        else:
            events.put({"type": "stopped", "count": completed})
    except Exception as exc:
        events.put({"type": "error", "message": str(exc), "count": completed})


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
        "适合文本编辑器等多行区域；Tab 与 Enter 会作为实体按键发送。",
    ),
    "代码编辑器": Scene(
        "代码编辑器",
        5,
        15,
        "保留空格与 Tab 缩进；建议先关闭目标编辑器的自动补全或自动缩进。",
    ),
    "网页输入框": Scene(
        "网页输入框",
        5,
        25,
        "多行内容请聚焦到多行编辑区；Enter 的换行或提交行为由网页决定。",
    ),
    "终端 / 命令行": Scene(
        "终端 / 命令行",
        5,
        40,
        "Enter 可能执行命令，请先检查内容；部分终端会自行处理 Tab 补全。",
    ),
    "远程桌面 / 虚拟机": Scene(
        "远程桌面 / 虚拟机",
        7,
        80,
        "较慢的输入速度，适合远程桌面或虚拟机；目标端仍需支持键盘事件。",
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
        self.countdown_text = tk.StringVar(value="5")
        self.interval_text = tk.StringVar(value="20")
        self.scene_hint = tk.StringVar(value=SCENES["通用窗口"].hint)
        self.stats_text = tk.StringVar()
        self.status_text = tk.StringVar(
            value="准备就绪。点击运行后，请在倒计时内切换到目标窗口并定位光标。"
        )

        self.root.title("逐字输入助手")
        self.root.geometry("900x760")
        self.root.minsize(820, 700)
        self.root.configure(bg=self.BG)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.configure_styles()
        self.build_ui()
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
        style.configure("Horizontal.TProgressbar", background=self.ACCENT, troughcolor=self.BORDER)

    def build_ui(self) -> None:
        outer = ttk.Frame(self.root, style="TFrame", padding=(24, 20))
        outer.pack(fill="both", expand=True)

        ttk.Label(outer, text="逐字输入助手", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            outer,
            text="模拟真实键盘逐字输入，不经过系统粘贴，完整保留中文、空格、缩进与换行。",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(4, 16))

        body = ttk.Frame(outer, style="TFrame")
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=3)
        body.rowconfigure(1, weight=0)
        body.rowconfigure(2, weight=2)
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
        ttk.Label(settings_card, textvariable=self.scene_hint, style="CardMuted.TLabel").grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(6, 12)
        )

        ttk.Label(settings_card, text="开始前等待", style="Card.TLabel").grid(row=2, column=0, sticky="w")
        self.countdown_spin = ttk.Spinbox(
            settings_card,
            from_=0,
            to=60,
            increment=1,
            width=6,
            textvariable=self.countdown_text,
            justify="center",
        )
        self.countdown_spin.grid(row=2, column=1, sticky="w", padx=(10, 0))
        ttk.Label(settings_card, text="秒", style="CardMuted.TLabel").grid(row=2, column=2, sticky="w", padx=(6, 24))

        ttk.Label(settings_card, text="字符间隔", style="Card.TLabel").grid(row=3, column=0, sticky="w", pady=(8, 0))
        self.interval_spin = ttk.Spinbox(
            settings_card,
            from_=0,
            to=1000,
            increment=5,
            width=6,
            textvariable=self.interval_text,
            justify="center",
        )
        self.interval_spin.grid(row=3, column=1, sticky="w", padx=(10, 0), pady=(8, 0))
        ttk.Label(settings_card, text="毫秒", style="CardMuted.TLabel").grid(
            row=3, column=2, sticky="w", padx=(6, 24), pady=(8, 0)
        )

        self.run_button = ttk.Button(
            settings_card,
            text="运行输入",
            style="Accent.TButton",
            command=self.start_typing,
        )
        self.run_button.grid(row=2, column=3, rowspan=2, sticky="e", padx=(16, 0))

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
        return normalize_newlines(self.editor.get("1.0", "end-1c"))

    def update_content_analysis(self) -> None:
        if self.worker is not None and self.worker.is_alive():
            return
        text = self.current_text()
        counts = content_counts(text)
        self.stats_text.set(
            f"共 {counts['total']} 字符    英文 {counts['english']}    中文 {counts['chinese']}    "
            f"空格 {counts['spaces']}    Tab {counts['tabs']}    换行 {counts['newlines']}"
        )
        self.preview.configure(state="normal")
        self.preview.delete("1.0", "end")
        self.preview.insert("1.0", visible_whitespace(text))
        self.preview.configure(state="disabled")

    def read_clipboard(self) -> None:
        try:
            text = normalize_newlines(self.root.clipboard_get())
        except tk.TclError:
            messagebox.showinfo("无法读取", "系统剪切板中没有可读取的文本。", parent=self.root)
            return
        self.editor.delete("1.0", "end")
        self.editor.insert("1.0", text)
        self.update_content_analysis()
        self.status_text.set("已读取系统剪切板，请选择场景并检查内容。")

    def apply_scene(self, _event=None) -> None:
        scene = SCENES[self.scene_name.get()]
        self.countdown_text.set(str(scene.countdown))
        self.interval_text.set(str(scene.interval))
        self.scene_hint.set(scene.hint)

    def set_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        self.read_button.configure(state=state)
        self.scene_combo.configure(state="disabled" if busy else "readonly")
        self.countdown_spin.configure(state=state)
        self.interval_spin.configure(state=state)
        self.editor.configure(state=state)
        if busy:
            self.run_button.configure(text="停止输入", style="Danger.TButton", command=self.request_stop)
        else:
            self.run_button.configure(text="运行输入", style="Accent.TButton", command=self.start_typing)

    def start_typing(self) -> None:
        text = self.current_text()
        if not text:
            messagebox.showwarning("无法运行", "请先粘贴或输入要发送的内容。", parent=self.root)
            return
        if "\x00" in text:
            messagebox.showwarning("无法运行", "内容包含无法输入的空字符，请移除后再运行。", parent=self.root)
            return

        try:
            countdown = int(self.countdown_text.get())
            interval = int(self.interval_text.get())
        except ValueError:
            messagebox.showwarning("参数无效", "等待时间和字符间隔必须填写整数。", parent=self.root)
            return
        if not 0 <= countdown <= 60 or not 0 <= interval <= 1000:
            messagebox.showwarning("参数无效", "等待时间需为 0–60 秒，字符间隔需为 0–1000 毫秒。", parent=self.root)
            return
        if not IS_WINDOWS:
            messagebox.showerror("不支持的系统", "此工具目前仅支持 Windows。", parent=self.root)
            return

        self.total_chars = len(text)
        self.progress.configure(maximum=self.total_chars, value=0)
        self.stop_event = threading.Event()
        self.events = queue.Queue()
        self.worker = threading.Thread(
            target=input_worker,
            args=(text, countdown, interval, self.stop_event, self.events),
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
        self.set_busy(False)
        kind = event["type"]
        count = event.get("count", 0)
        if kind == "done":
            self.progress.configure(value=self.total_chars)
            self.status_text.set(f"输入完成，共输入 {count} 个字符。")
        elif kind == "stopped":
            self.progress.configure(value=count)
            self.status_text.set(f"已停止，已输入 {count} / {self.total_chars} 个字符。")
        elif kind == "error":
            self.progress.configure(value=count)
            message = f"已输入 {count} 个字符。{event['message']}"
            self.status_text.set("输入失败，请查看错误信息。")
            messagebox.showerror(
                "输入失败",
                message
                + "\n\n如果目标程序以管理员权限运行，请尝试以管理员身份运行本工具。",
                parent=self.root,
            )
        self.worker = None

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
                self.status_text.set("正在逐字输入… 按 F8 可停止。")
            elif kind == "progress":
                count = event["count"]
                percent = int(count * 100 / self.total_chars) if self.total_chars else 100
                self.progress.configure(value=count)
                self.status_text.set(f"正在输入 {count} / {self.total_chars}（{percent}%）· 按 F8 可停止。")
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
    root = tk.Tk()
    AutoTyperApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
