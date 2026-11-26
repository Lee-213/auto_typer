from __future__ import annotations

import ctypes
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass

IS_WINDOWS = sys.platform == "win32"
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002


class ClipboardError(OSError):
    pass


@dataclass(frozen=True)
class ClipboardSnapshot:
    text: str | None
    sequence_number: int


if IS_WINDOWS:
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.argtypes = []
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
    user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = wintypes.HANDLE
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE
    user32.GetClipboardSequenceNumber.argtypes = []
    user32.GetClipboardSequenceNumber.restype = wintypes.DWORD
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalFree.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalUnlock.restype = wintypes.BOOL
    kernel32.GlobalSize.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalSize.restype = ctypes.c_size_t
else:
    user32 = None
    kernel32 = None


def _require_windows() -> None:
    if not IS_WINDOWS or user32 is None or kernel32 is None:
        raise ClipboardError("当前平台不支持 Windows 剪贴板操作")


@contextmanager
def _clipboard_session():
    _require_windows()
    for attempt in range(8):
        if user32.OpenClipboard(None):
            break
        if attempt == 7:
            error = ctypes.get_last_error()
            detail = ctypes.FormatError(error) if error else "剪贴板正被其他程序占用"
            raise ClipboardError(f"无法打开 Windows 剪贴板：{detail}")
        time.sleep(0.02)
    try:
        yield
    finally:
        user32.CloseClipboard()


def _clipboard_sequence_number() -> int:
    _require_windows()
    return int(user32.GetClipboardSequenceNumber())


def read_clipboard_text() -> str | None:
    _require_windows()
    with _clipboard_session():
        if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
            return None
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            error = ctypes.get_last_error()
            raise ClipboardError(f"无法读取剪贴板文本：{ctypes.FormatError(error)}")
        locked = kernel32.GlobalLock(handle)
        if not locked:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            size = kernel32.GlobalSize(handle)
            if size < 2:
                return ""
            return ctypes.wstring_at(locked, size // 2 - 1)
        finally:
            kernel32.GlobalUnlock(handle)


def capture_clipboard_text() -> ClipboardSnapshot:
    return ClipboardSnapshot(
        text=read_clipboard_text(),
        sequence_number=_clipboard_sequence_number(),
    )


def write_clipboard_text(text: str) -> int:
    if not isinstance(text, str):
        raise TypeError("剪贴板文本必须是字符串")
    _require_windows()
    payload = text.encode("utf-16-le") + b"\x00\x00"
    with _clipboard_session():
        if not user32.EmptyClipboard():
            raise ctypes.WinError(ctypes.get_last_error())
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(payload))
        if not handle:
            raise MemoryError("无法分配剪贴板文本内存")
        locked = kernel32.GlobalLock(handle)
        if not locked:
            kernel32.GlobalFree(handle)
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            ctypes.memmove(locked, payload, len(payload))
        finally:
            kernel32.GlobalUnlock(handle)
        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            kernel32.GlobalFree(handle)
            raise ctypes.WinError(ctypes.get_last_error())
        # SetClipboardData 成功后内存所有权转移给系统，不能再次 GlobalFree。
    return _clipboard_sequence_number()


def restore_clipboard_text(
    snapshot: ClipboardSnapshot,
    expected_sequence: int | None,
) -> tuple[bool, str]:
    if snapshot.text is None:
        return False, "原剪贴板没有可恢复的 Unicode 文本，保留本次输入文本"
    if expected_sequence is not None and _clipboard_sequence_number() != expected_sequence:
        return False, "剪贴板序列已变化，为避免覆盖新内容而不恢复原文本"
    try:
        write_clipboard_text(snapshot.text)
    except Exception as exc:
        return False, f"恢复原剪贴板文本失败：{exc}"
    return True, "已恢复原剪贴板文本"
