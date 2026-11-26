from __future__ import annotations

import ctypes
import sys
import time
import uuid
from dataclasses import dataclass

from text_parser import compare_text, normalize_newlines

IS_WINDOWS = sys.platform == "win32"
UIA_E_NOTSUPPORTED = 0x80040200
UIA_ValuePatternId = 10002
UIA_TextPatternId = 10014
UIA_ControlTypePropertyId = 30003
UIA_IsEnabledPropertyId = 30010
UIA_IsPasswordPropertyId = 30019
UIA_ProcessIdPropertyId = 30002
UIA_NativeWindowHandlePropertyId = 30020
VT_EMPTY = 0
VT_I4 = 3
VT_BSTR = 8
VT_BOOL = 11

CLSID_CUIAutomation = "ff48dba4-60ef-4201-aa87-54103eef594e"
IID_IUIAutomation = "30cbe57d-d9d0-452a-ab13-7ac5ac4825ee"
IID_IUIAutomationValuePattern = "a94cd8b1-0844-4cd6-9d2d-640537ab39e9"
IID_IUIAutomationTextPattern = "32eba289-3583-42c9-9c59-3b6d9a1e9b6a"


class TargetUnavailable(OSError):
    pass


class TargetChanged(TargetUnavailable):
    pass


@dataclass(frozen=True)
class TargetSnapshot:
    readable: bool
    text: str | None
    start: int | None
    end: int | None
    direct_writable: bool
    reason: str
    control_type: int | None = None
    process_id: int | None = None
    window_handle: int | None = None
    is_password: bool = False
    is_enabled: bool = True
    value_pattern_supported: bool = False
    text_pattern_supported: bool = False
    value_read_only: bool | None = None

    def as_dict(self) -> dict:
        return {
            "readable": self.readable,
            "direct_writable": self.direct_writable,
            "reason": self.reason,
            "start": self.start,
            "end": self.end,
            "control_type": self.control_type,
            "process_id": self.process_id,
            "window_handle": self.window_handle,
            "is_password": self.is_password,
            "is_enabled": self.is_enabled,
            "value_pattern_supported": self.value_pattern_supported,
            "text_pattern_supported": self.text_pattern_supported,
            "value_read_only": self.value_read_only,
        }


@dataclass(frozen=True)
class Verification:
    status: str
    method: str
    reason: str
    readable: bool = False
    exact_match: bool | None = None
    focus_same: bool | None = None
    caret_restored: bool | None = None
    comparison: dict | None = None

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "method": self.method,
            "reason": self.reason,
            "readable": self.readable,
            "exact_match": self.exact_match,
            "focus_same": self.focus_same,
            "caret_restored": self.caret_restored,
            "comparison": self.comparison,
        }


if IS_WINDOWS:
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", ctypes.c_ulong),
            ("Data2", ctypes.c_ushort),
            ("Data3", ctypes.c_ushort),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    class VariantValue(ctypes.Union):
        _fields_ = [
            ("llVal", ctypes.c_longlong),
            ("lVal", ctypes.c_long),
            ("bstrVal", ctypes.c_void_p),
            ("punkVal", ctypes.c_void_p),
        ]
        if ctypes.sizeof(ctypes.c_void_p) == 8:
            _fields_.append(("reserved", ctypes.c_ubyte * 16))

    class VARIANT(ctypes.Structure):
        _fields_ = [
            ("vt", ctypes.c_ushort),
            ("wReserved1", ctypes.c_ushort),
            ("wReserved2", ctypes.c_ushort),
            ("wReserved3", ctypes.c_ushort),
            ("value", VariantValue),
        ]

    ole32 = ctypes.WinDLL("ole32", use_last_error=True)
    oleaut32 = ctypes.WinDLL("oleaut32", use_last_error=True)
    ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    ole32.CoInitializeEx.restype = ctypes.c_long
    ole32.CoUninitialize.argtypes = []
    ole32.CoUninitialize.restype = None
    ole32.CoCreateInstance.argtypes = [
        ctypes.POINTER(GUID),
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(GUID),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    ole32.CoCreateInstance.restype = ctypes.c_long
    oleaut32.SysAllocString.argtypes = [ctypes.c_wchar_p]
    oleaut32.SysAllocString.restype = ctypes.c_void_p
    oleaut32.SysFreeString.argtypes = [ctypes.c_void_p]
    oleaut32.SysFreeString.restype = None
    oleaut32.SafeArrayAccessData.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.POINTER(ctypes.c_int)),
    ]
    oleaut32.SafeArrayAccessData.restype = ctypes.c_long
    oleaut32.SafeArrayUnaccessData.argtypes = [ctypes.c_void_p]
    oleaut32.SafeArrayUnaccessData.restype = ctypes.c_long
    oleaut32.SafeArrayGetDim.argtypes = [ctypes.c_void_p]
    oleaut32.SafeArrayGetDim.restype = wintypes.UINT
    oleaut32.SafeArrayGetLBound.argtypes = [ctypes.c_void_p, wintypes.UINT]
    oleaut32.SafeArrayGetLBound.restype = ctypes.c_long
    oleaut32.SafeArrayGetUBound.argtypes = [ctypes.c_void_p, wintypes.UINT]
    oleaut32.SafeArrayGetUBound.restype = ctypes.c_long
    oleaut32.SafeArrayDestroy.argtypes = [ctypes.c_void_p]
    oleaut32.SafeArrayDestroy.restype = ctypes.c_long


def _guid(value: str) -> ctypes.Structure:
    return GUID.from_buffer_copy(uuid.UUID(value).bytes_le)


def _raw_offset_from_normalized(text: str, offset: int) -> int:
    normalized_length = len(normalize_newlines(text))
    if not 0 <= offset <= normalized_length:
        raise ValueError("规范化文本偏移超出范围")
    raw_index = 0
    normalized_index = 0
    while normalized_index < offset:
        if text[raw_index] == "\r":
            raw_index += 1
            if raw_index < len(text) and text[raw_index] == "\n":
                raw_index += 1
        else:
            raw_index += 1
        normalized_index += 1
    return raw_index


def _method(pointer: int, index: int, restype, *argtypes):
    if not pointer:
        raise TargetUnavailable("UI Automation 返回了空接口指针")
    vtable = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(vtable[index])


def _check(hr: int, operation: str) -> None:
    if hr < 0:
        code = hr & 0xFFFFFFFF
        detail = ctypes.FormatError(code)
        raise OSError(code, f"{operation}失败（HRESULT 0x{code:08X}）：{detail}")


def _release(pointer: int | None) -> None:
    if pointer:
        try:
            _method(pointer, 2, wintypes.ULONG)(pointer)
        except Exception:
            pass


def _read_bstr(pointer: int | None) -> str:
    if not pointer:
        return ""
    try:
        return ctypes.wstring_at(pointer)
    finally:
        oleaut32.SysFreeString(pointer)


def _safe_array_ints(pointer: int | None) -> tuple[int, ...]:
    if not pointer:
        return ()
    data = ctypes.POINTER(ctypes.c_int)()
    accessed = False
    try:
        _check(
            oleaut32.SafeArrayAccessData(pointer, ctypes.byref(data)),
            "读取 SAFEARRAY",
        )
        accessed = True
        if oleaut32.SafeArrayGetDim(pointer) != 1:
            return ()
        lower = oleaut32.SafeArrayGetLBound(pointer, 1)
        upper = oleaut32.SafeArrayGetUBound(pointer, 1)
        return tuple(data[index - lower] for index in range(lower, upper + 1))
    finally:
        if accessed:
            oleaut32.SafeArrayUnaccessData(pointer)
        oleaut32.SafeArrayDestroy(pointer)


class TargetController:
    """在单一 MTA 工作线程中读取焦点控件，并执行可验证的整值写入。"""

    def __init__(self) -> None:
        self._automation = 0
        self._element = 0
        self._runtime_id: tuple[int, ...] = ()
        self._snapshot: TargetSnapshot | None = None
        self._com_initialized = False
        self.write_attempted = False

    def __enter__(self) -> TargetController:
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.close()

    def _initialize(self) -> None:
        if not IS_WINDOWS:
            raise TargetUnavailable("当前平台不支持 UI Automation 目标读取")
        hr = ole32.CoInitializeEx(None, 0)
        if hr not in (0, 1):
            _check(hr, "初始化 COM MTA")
        self._com_initialized = True

        clsid = _guid(CLSID_CUIAutomation)
        iid = _guid(IID_IUIAutomation)
        automation = ctypes.c_void_p()
        _check(
            ole32.CoCreateInstance(
                ctypes.byref(clsid),
                None,
                1,
                ctypes.byref(iid),
                ctypes.byref(automation),
            ),
            "创建 UI Automation 客户端",
        )
        self._automation = automation.value

    def _property(self, property_id: int):
        value = VARIANT()
        _check(
            _method(self._element, 10, ctypes.c_long, ctypes.c_int, ctypes.POINTER(VARIANT))(
                self._element, property_id, ctypes.byref(value)
            ),
            "读取控件属性",
        )
        return value

    def _int_property(self, property_id: int) -> int | None:
        value = self._property(property_id)
        if value.vt == VT_I4:
            return int(value.value.lVal)
        return None

    def _bool_property(self, property_id: int, default: bool) -> bool:
        value = self._property(property_id)
        if value.vt == VT_BOOL:
            return bool(value.value.lVal)
        return default

    def _pattern(self, pattern_id: int, interface_id: str) -> int:
        iid = _guid(interface_id)
        pattern = ctypes.c_void_p()
        hr = _method(
            self._element,
            14,
            ctypes.c_long,
            ctypes.c_int,
            ctypes.POINTER(GUID),
            ctypes.POINTER(ctypes.c_void_p),
        )(self._element, pattern_id, ctypes.byref(iid), ctypes.byref(pattern))
        if hr == UIA_E_NOTSUPPORTED:
            return 0
        _check(hr, "获取控件模式")
        return pattern.value

    def _focused_element(self) -> int:
        element = ctypes.c_void_p()
        _check(
            _method(self._automation, 8, ctypes.c_long, ctypes.POINTER(ctypes.c_void_p))(
                self._automation, ctypes.byref(element)
            ),
            "获取焦点控件",
        )
        return element.value

    def _value_state(self) -> tuple[str | None, bool | None]:
        pattern = self._pattern(UIA_ValuePatternId, IID_IUIAutomationValuePattern)
        if not pattern:
            return None, None
        try:
            value_pointer = ctypes.c_void_p()
            _check(
                _method(pattern, 4, ctypes.c_long, ctypes.POINTER(ctypes.c_void_p))(
                    pattern, ctypes.byref(value_pointer)
                ),
                "读取控件值",
            )
            value = _read_bstr(value_pointer.value)
            read_only = ctypes.c_int()
            _check(
                _method(pattern, 5, ctypes.c_long, ctypes.POINTER(ctypes.c_int))(
                    pattern, ctypes.byref(read_only)
                ),
                "读取控件只读状态",
            )
            return value, bool(read_only.value)
        finally:
            _release(pattern)

    def _document_range(self) -> int:
        pattern = self._pattern(UIA_TextPatternId, IID_IUIAutomationTextPattern)
        if not pattern:
            return 0
        try:
            document = ctypes.c_void_p()
            _check(
                _method(pattern, 7, ctypes.c_long, ctypes.POINTER(ctypes.c_void_p))(
                    pattern, ctypes.byref(document)
                ),
                "读取文档文本范围",
            )
            return document.value
        finally:
            _release(pattern)

    @staticmethod
    def _range_text(text_range: int) -> str:
        text_pointer = ctypes.c_void_p()
        _check(
            _method(text_range, 12, ctypes.c_long, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p))(
                text_range, -1, ctypes.byref(text_pointer)
            ),
            "读取文本范围",
        )
        return _read_bstr(text_pointer.value)

    @staticmethod
    def _range_clone(text_range: int) -> int:
        clone = ctypes.c_void_p()
        _check(
            _method(text_range, 3, ctypes.c_long, ctypes.POINTER(ctypes.c_void_p))(
                text_range, ctypes.byref(clone)
            ),
            "复制文本范围",
        )
        return clone.value

    def _selection_offsets(self, document_text: str) -> tuple[int, int] | None:
        pattern = self._pattern(UIA_TextPatternId, IID_IUIAutomationTextPattern)
        if not pattern:
            return None
        ranges = 0
        try:
            ranges_pointer = ctypes.c_void_p()
            _check(
                _method(pattern, 5, ctypes.c_long, ctypes.POINTER(ctypes.c_void_p))(
                    pattern, ctypes.byref(ranges_pointer)
                ),
                "读取目标选区",
            )
            ranges = ranges_pointer.value
            length = ctypes.c_int()
            _check(
                _method(ranges, 3, ctypes.c_long, ctypes.POINTER(ctypes.c_int))(
                    ranges, ctypes.byref(length)
                ),
                "读取选区数量",
            )
            if length.value != 1:
                return None
            selection = ctypes.c_void_p()
            _check(
                _method(ranges, 4, ctypes.c_long, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p))(
                    ranges, 0, ctypes.byref(selection)
                ),
                "读取选区范围",
            )
            try:
                selected_text = self._range_text(selection.value)
                document = self._document_range()
                try:
                    prefix = self._range_clone(document)
                    try:
                        _check(
                            _method(
                                prefix,
                                15,
                                ctypes.c_long,
                                ctypes.c_int,
                                ctypes.c_void_p,
                                ctypes.c_int,
                            )(prefix, 0, selection.value, 0),
                            "定位选区起点",
                        )
                        start = len(self._range_text(prefix))
                    finally:
                        _release(prefix)

                    end_prefix = self._range_clone(document)
                    try:
                        _check(
                            _method(
                                end_prefix,
                                15,
                                ctypes.c_long,
                                ctypes.c_int,
                                ctypes.c_void_p,
                                ctypes.c_int,
                            )(end_prefix, 0, selection.value, 1),
                            "定位选区终点",
                        )
                        end = len(self._range_text(end_prefix))
                    finally:
                        _release(end_prefix)
                finally:
                    _release(document)

                if not 0 <= start <= end <= len(document_text):
                    return None
                if document_text[start:end] != selected_text:
                    return None
                return start, end
            finally:
                _release(selection.value)
        finally:
            _release(ranges)

    def _read_current(self) -> TargetSnapshot:
        value, read_only = self._value_state()
        value_pattern_supported = value is not None or read_only is not None
        document_text = None
        text_pattern_supported = False
        start = None
        end = None
        document = self._document_range()
        try:
            if document:
                document_text = self._range_text(document)
                text_pattern_supported = True
        finally:
            _release(document)

        if document_text is not None:
            offsets = self._selection_offsets(document_text)
            if offsets is not None:
                start, end = offsets

        if value is not None:
            text = value
            if document_text is not None and value != document_text:
                start = None
                end = None
        else:
            text = document_text

        control_type = self._int_property(UIA_ControlTypePropertyId)
        process_id = self._int_property(UIA_ProcessIdPropertyId)
        window_handle = self._int_property(UIA_NativeWindowHandlePropertyId)
        is_enabled = self._bool_property(UIA_IsEnabledPropertyId, True)
        is_password = self._bool_property(UIA_IsPasswordPropertyId, False)

        if text is None:
            reason = "焦点控件不支持 ValuePattern 或 TextPattern，无法读取最终文本"
            direct = False
        elif not is_enabled:
            reason = "焦点控件已禁用"
            direct = False
        elif is_password:
            reason = "焦点控件是密码框，禁止整值直写"
            direct = False
        elif value is None or read_only is not False:
            reason = "焦点控件不支持可写 ValuePattern"
            direct = False
        elif start is None or end is None:
            reason = "无法确定唯一选区，避免覆盖目标内容"
            direct = False
        else:
            reason = "支持 ValuePattern 整值写入，并在写入后读回校验"
            direct = True

        return TargetSnapshot(
            readable=text is not None,
            text=text,
            start=start,
            end=end,
            direct_writable=direct,
            reason=reason,
            control_type=control_type,
            process_id=process_id,
            window_handle=window_handle,
            is_password=is_password,
            is_enabled=is_enabled,
            value_pattern_supported=value_pattern_supported,
            text_pattern_supported=text_pattern_supported,
            value_read_only=read_only,
        )

    def capture(self) -> TargetSnapshot:
        if not self._automation:
            self._initialize()
        element = self._focused_element()
        if not element:
            raise TargetUnavailable("没有获得焦点控件")
        _release(self._element)
        self._element = element
        runtime_id = ctypes.c_void_p()
        _check(
            _method(self._element, 4, ctypes.c_long, ctypes.POINTER(ctypes.c_void_p))(
                self._element, ctypes.byref(runtime_id)
            ),
            "读取控件标识",
        )
        self._runtime_id = _safe_array_ints(runtime_id.value)
        self._snapshot = self._read_current()
        return self._snapshot

    @property
    def snapshot(self) -> TargetSnapshot | None:
        return self._snapshot

    def expected_text(self, insertion: str) -> str | None:
        snapshot = self._snapshot
        if snapshot is None or snapshot.text is None:
            return None
        if snapshot.start is None or snapshot.end is None:
            return insertion if snapshot.text == "" else None
        return snapshot.text[: snapshot.start] + insertion + snapshot.text[snapshot.end :]

    def _same_focus(self) -> bool | None:
        current = 0
        try:
            current = self._focused_element()
            same = ctypes.c_int()
            _check(
                _method(self._automation, 3, ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int))(
                    self._automation, self._element, current, ctypes.byref(same)
                ),
                "比较焦点控件",
            )
            return bool(same.value)
        except Exception:
            return None
        finally:
            _release(current)

    def write_exact(self, insertion: str) -> Verification:
        snapshot = self._snapshot
        if snapshot is None or not snapshot.direct_writable:
            raise TargetUnavailable("当前控件不支持安全的整值直写")
        expected = self.expected_text(insertion)
        if expected is None:
            raise TargetUnavailable("无法计算写入后的完整目标文本")
        current = self._read_current()
        if (
            current.text != snapshot.text
            or current.start != snapshot.start
            or current.end != snapshot.end
        ):
            raise TargetChanged("写入前目标文本或选区已变化，已取消直写")

        pattern = self._pattern(UIA_ValuePatternId, IID_IUIAutomationValuePattern)
        if not pattern:
            raise TargetUnavailable("写入前控件丢失 ValuePattern")
        value = oleaut32.SysAllocString(ctypes.c_wchar_p(expected))
        try:
            if not value:
                raise MemoryError("无法分配 UI Automation BSTR")
            self.write_attempted = True
            _check(
                _method(pattern, 3, ctypes.c_long, ctypes.c_void_p)(pattern, value),
                "写入控件值",
            )
        finally:
            _release(pattern)
            if value:
                oleaut32.SysFreeString(value)

        time.sleep(0.05)
        actual = self._read_current().text
        caret_restored = None
        comparison = compare_text(expected, actual) if actual is not None else None
        if comparison is not None and comparison["exact_match"]:
            normalized_offset = len(
                normalize_newlines(expected[: snapshot.start + len(insertion)])
            )
            actual_offset = _raw_offset_from_normalized(actual, normalized_offset)
            caret_restored = self._restore_caret(actual_offset)
        if actual is None:
            return Verification(
                status="unverified",
                method="uia_value",
                reason="整值写入调用成功，但控件不支持读回，无法确认最终文本",
            )
        return Verification(
            status="verified" if comparison["exact_match"] else "mismatch",
            method="uia_value",
            reason="目标文本读回一致" if comparison["exact_match"] else "目标文本读回后与预期不一致",
            readable=True,
            exact_match=comparison["exact_match"],
            focus_same=self._same_focus(),
            caret_restored=caret_restored,
            comparison=comparison,
        )

    def _restore_caret(self, offset: int) -> bool:
        document = self._document_range()
        if not document:
            return False
        caret = 0
        try:
            caret = self._range_clone(document)
            document_length = len(self._range_text(document))
            _check(
                _method(
                    caret,
                    14,
                    ctypes.c_long,
                    ctypes.c_int,
                    ctypes.c_int,
                    ctypes.c_int,
                    ctypes.POINTER(ctypes.c_int),
                )(caret, 1, 0, offset - document_length, ctypes.byref(ctypes.c_int())),
                "恢复光标位置",
            )
            _check(_method(caret, 16, ctypes.c_long)(caret), "设置光标位置")
            offsets = self._selection_offsets(self._range_text(document))
            return offsets == (offset, offset)
        except Exception:
            return False
        finally:
            _release(caret)
            _release(document)

    def verify(self, expected: str | None, method: str = "keyboard") -> Verification:
        snapshot = self._snapshot
        if expected is None or snapshot is None or snapshot.text is None:
            return Verification(
                status="unverified",
                method=method,
                reason="目标不支持完整文本读取或选区定位；输入完成不等于最终文本一致",
            )
        actual = self._read_current().text
        if actual is None:
            return Verification(
                status="unverified",
                method=method,
                reason="输入后无法读取目标文本，最终结果无法验证",
            )
        comparison = compare_text(expected, actual)
        return Verification(
            status="verified" if comparison["exact_match"] else "mismatch",
            method=method,
            reason="目标文本读回一致" if comparison["exact_match"] else "目标文本读回后与预期不一致",
            readable=True,
            exact_match=comparison["exact_match"],
            focus_same=self._same_focus(),
            comparison=comparison,
        )

    def close(self) -> None:
        _release(self._element)
        self._element = 0
        _release(self._automation)
        self._automation = 0
        if self._com_initialized:
            self._com_initialized = False
            ole32.CoUninitialize()
