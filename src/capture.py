# -*- coding: utf-8 -*-
"""取词：把"用户选中的文字"弄到手。

为什么不用 Windows UI Automation？
----------------------------------
UIA 看起来很正统，但 Chrome / Electron 系应用（Chrome、Edge、VS Code、
以及各类现代编辑器）的无障碍接口默认是**惰性开启**的：第一次去问它要选区，
要么返回空，要么现场初始化拖几百毫秒。老程序里干脆不支持的也有一大批。

这里走的是"复刻用户自己按 Ctrl+C"这条路：
    释放按住的修饰键 -> 发送 Ctrl+C -> 等剪贴板更新 -> 读出来 -> 还原原内容
只要那个程序能复制，这里就能拿到 —— 兼容性天花板最高。

两个细节保证"无感"：
  1. 用 GetClipboardSequenceNumber 的序号变化来判断复制是否真的发生了，
     而不是往剪贴板里塞哨兵值（后者会污染 Win+V 剪贴板历史）。
  2. 提前释放 Ctrl/Shift/Alt/Win，避免用户"正按着 Shift 划词"时被拼成
     Ctrl+Shift+C，在浏览器和终端里触发别的功能。
"""

from __future__ import annotations

import ctypes
import logging
import threading
import time
from ctypes import wintypes

import keyboard  # 全局键盘钩子，用来注册热键和模拟按键

log = logging.getLogger(__name__)

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

CF_TEXT = 1
CF_BITMAP = 2
CF_METAFILEPICT = 3
CF_OEMTEXT = 7
CF_DIB = 8
CF_PALETTE = 9
CF_UNICODETEXT = 13
CF_ENHMETAFILE = 14
CF_HDROP = 15
CF_LOCALE = 16
CF_DIBV5 = 17
GMEM_MOVEABLE = 0x0002

# 同一次复制可能被划词预检测和翻译流程同时触发，串行化避免互相读到半截
_COPY_LOCK = threading.Lock()

# ---- 函数签名声明：64 位下不声明会截断指针，必须写全 ----
user32.OpenClipboard.argtypes = [wintypes.HWND]
user32.OpenClipboard.restype = wintypes.BOOL
user32.CloseClipboard.argtypes = []
user32.CloseClipboard.restype = wintypes.BOOL
user32.EmptyClipboard.argtypes = []
user32.EmptyClipboard.restype = wintypes.BOOL
user32.GetClipboardData.argtypes = [wintypes.UINT]
user32.GetClipboardData.restype = wintypes.HANDLE
user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
user32.SetClipboardData.restype = wintypes.HANDLE
user32.GetClipboardSequenceNumber.argtypes = []
user32.GetClipboardSequenceNumber.restype = wintypes.DWORD
user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
user32.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]
user32.RegisterClipboardFormatW.restype = wintypes.UINT

kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalLock.restype = wintypes.LPVOID
kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalUnlock.restype = wintypes.BOOL
kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalFree.restype = wintypes.HGLOBAL
kernel32.GlobalSize.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalSize.restype = ctypes.c_size_t

# 需要提前松开的修饰键
_MODIFIERS = (
    "left ctrl",
    "right ctrl",
    "left shift",
    "right shift",
    "left alt",
    "right alt",
    "left windows",
    "right windows",
)


class CaptureError(Exception):
    """取词失败，消息直接展示给用户。"""


# ---------------------------------------------------------------- 剪贴板底层


def _open_clipboard(retries: int = 10, delay: float = 0.02) -> bool:
    """剪贴板可能正被别的程序占用，重试几次再放弃。"""
    for _ in range(retries):
        if user32.OpenClipboard(None):
            return True
        time.sleep(delay)
    return False


def get_clipboard_text() -> str | None:
    """读剪贴板里的 Unicode 文本。里面不是文本（图片/文件）时返回 None。"""
    if not _open_clipboard():
        return None
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return None
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            return None
        try:
            return ctypes.c_wchar_p(pointer).value
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def set_clipboard_text(text: str) -> bool:
    """把文本写进剪贴板。写入成功后内存归系统所有，不能再手动释放。"""
    if not _open_clipboard():
        return False
    try:
        if not user32.EmptyClipboard():
            return False

        size = (len(text) + 1) * ctypes.sizeof(ctypes.c_wchar)
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
        if not handle:
            return False

        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            kernel32.GlobalFree(handle)
            return False
        try:
            ctypes.memmove(pointer, ctypes.create_unicode_buffer(text), size)
        finally:
            kernel32.GlobalUnlock(handle)

        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            kernel32.GlobalFree(handle)
            return False
        return True
    finally:
        user32.CloseClipboard()


def clipboard_sequence() -> int:
    """剪贴板的修改序号。只要有人往里写过东西，这个数就会变。"""
    return int(user32.GetClipboardSequenceNumber())


def clipboard_has_text() -> bool:
    return get_clipboard_text() is not None


# ---------------------------------------------------------------- 剪贴板完整备份


def _register_format(name: str) -> int:
    try:
        return int(user32.RegisterClipboardFormatW(name))
    except Exception:  # noqa: BLE001
        return 0


# 只备份文本类格式。图片/文件等无法安全还原的内容出现时，预检测直接跳过，
# 宁可这次不弹按钮，也不能把用户的剪贴板弄丢。
_TEXT_FORMATS = tuple(
    fmt for fmt in (
        CF_UNICODETEXT,
        CF_TEXT,
        CF_OEMTEXT,
        CF_LOCALE,
        _register_format("HTML Format"),
        _register_format("Rich Text Format"),
        _register_format("text/html"),
        _register_format("SourceURL"),
        _register_format("text/_moz_htmlinfo"),
        _register_format("text/_moz_htmlcontext"),
        _register_format("Chromium internal source RFH token"),
        _register_format("Chromium internal source URL"),
    )
    if fmt
)
_UNRESTORABLE_FORMATS = (
    CF_BITMAP, CF_METAFILEPICT, CF_PALETTE, CF_ENHMETAFILE,
    CF_HDROP, CF_DIB, CF_DIBV5,
)
_UNRESTORABLE_NAMES = tuple(
    fmt for fmt in (
        _register_format("PNG"),
        _register_format("image/png"),
        _register_format("image/jpeg"),
        _register_format("JFIF"),
        _register_format("GIF"),
        _register_format("image/gif"),
    )
    if fmt
)


def _clipboard_snapshot() -> dict[int, bytes] | None:
    """备份文本类剪贴板格式；遇到图片/文件等无法还原的格式时返回 None。"""
    if not _open_clipboard():
        return None
    try:
        for fmt in _UNRESTORABLE_FORMATS:
            if user32.IsClipboardFormatAvailable(fmt):
                return None
        for fmt in _UNRESTORABLE_NAMES:
            if user32.IsClipboardFormatAvailable(fmt):
                return None

        snapshot: dict[int, bytes] = {}
        for fmt in _TEXT_FORMATS:
            if not user32.IsClipboardFormatAvailable(fmt):
                continue
            handle = user32.GetClipboardData(fmt)
            if not handle:
                continue
            size = int(kernel32.GlobalSize(handle))
            if size <= 0:
                continue
            pointer = kernel32.GlobalLock(handle)
            if not pointer:
                continue
            try:
                snapshot[int(fmt)] = ctypes.string_at(pointer, size)
            finally:
                kernel32.GlobalUnlock(handle)
        return snapshot
    except Exception as exc:  # noqa: BLE001
        log.debug("备份剪贴板失败：%s", exc)
        return None
    finally:
        user32.CloseClipboard()


def _restore_clipboard(snapshot: dict[int, bytes]) -> None:
    if not _open_clipboard():
        log.warning("剪贴板被占用，预检测后没能还原")
        return
    try:
        user32.EmptyClipboard()
        for fmt, data in snapshot.items():
            handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, max(1, len(data)))
            if not handle:
                continue
            pointer = kernel32.GlobalLock(handle)
            if not pointer:
                kernel32.GlobalFree(handle)
                continue
            try:
                ctypes.memmove(pointer, data, len(data))
            finally:
                kernel32.GlobalUnlock(handle)
            if not user32.SetClipboardData(fmt, handle):
                kernel32.GlobalFree(handle)
    except Exception as exc:  # noqa: BLE001
        log.warning("还原剪贴板失败：%s", exc)
    finally:
        user32.CloseClipboard()


# ---------------------------------------------------------------- 取词


def _release_modifiers() -> None:
    for key in _MODIFIERS:
        try:
            keyboard.release(key)
        except Exception:  # noqa: BLE001 - keyboard 对未按下的键行为不一致，忽略即可
            pass


# 相邻按键之间的间隔。太短（比如 keyboard.send 那样一气呵成）时，
# 一部分程序来不及识别出组合键，会把 Ctrl+C 当成普通的字母 c。
_KEY_GAP = 0.012


def _send_copy() -> None:
    """分步发送 Ctrl+C，中途出错也保证 Ctrl 会被松开。

    这里不用 keyboard.send("ctrl+c")：它把按下与抬起压在一起，
    在老一点儿的 Win32 程序里经常识别不出修饰键。
    """
    try:
        keyboard.press("ctrl")
        time.sleep(_KEY_GAP)
        keyboard.press("c")
        time.sleep(_KEY_GAP)
        keyboard.release("c")
    finally:
        time.sleep(_KEY_GAP)
        keyboard.release("ctrl")


def _grab_selection(
    timeout: float = 0.55,
    poll_interval: float = 0.008,
    settle_before: float = 0.04,
    attempts: int = 2,
) -> str:
    """把当前选中的文字取回来。

    流程：备份剪贴板 -> 松修饰键 -> 记序号 -> 发 Ctrl+C -> 等序号变化 -> 读文本 -> 还原。

    attempts > 1 时，第一次没捞到会再试一次 —— 有些程序第一次响应偏慢。
    还原策略：原来有文本就还原文本；原来是图片/文件（我们备份不了）则保留当前文本，
    因为清空也救不回图片，反而让用户下一次 Ctrl+V 毫无反应、更困惑。
    """
    saved_text = get_clipboard_text()
    had_text = saved_text is not None

    _release_modifiers()
    time.sleep(settle_before)

    text: str | None = None
    for _ in range(max(1, attempts)):
        seq_before = clipboard_sequence()
        try:
            _send_copy()
        except Exception as exc:  # noqa: BLE001
            _restore(saved_text, had_text)
            raise CaptureError(f"模拟复制失败：{exc}") from exc

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            time.sleep(poll_interval)
            if clipboard_sequence() != seq_before:
                break
        else:
            continue  # 序号没动，这次白跑，进下一轮重试

        # 序号变了，但内容可能还没写完（大段文本时），给它一点落盘时间
        candidate = get_clipboard_text()
        if candidate is None:
            for _ in range(6):
                time.sleep(0.02)
                candidate = get_clipboard_text()
                if candidate is not None:
                    break
        if candidate and candidate.strip():
            text = candidate
            break

    _restore(saved_text, had_text)

    if text is None:
        raise CaptureError("没有取到文字：先选中一段英文再按快捷键")

    return text.strip()


def _looks_like_password_mask(text: str) -> bool:
    """浏览器密码框复制出来的是一串圆点/星号，别拿它弹翻译按钮。"""
    stripped = "".join(text.split())
    return len(stripped) >= 3 and all(ch in "•●○*" for ch in stripped)


def grab_selection(
    timeout: float = 0.55,
    poll_interval: float = 0.008,
    settle_before: float = 0.04,
    attempts: int = 2,
) -> str:
    """把当前选中的文字取回来（和划词预检测串行，避免同时按 Ctrl+C）。"""
    with _COPY_LOCK:
        return _grab_selection(timeout, poll_interval, settle_before, attempts)


def peek_selection(
    timeout: float = 0.6,
    poll_interval: float = 0.008,
    settle_before: float = 0.04,
) -> str | None:
    """临时复制当前选区，读到文字后把剪贴板按格式还原。

    只给划词按钮做"是否选中了文字"的预检测用：
      · 读不到文字 -> None
      · 剪贴板里有图片/文件等无法安全还原的内容 -> 直接跳过，返回 None
    返回的文字可以缓存下来，用户点按钮时不用再复制一次。
    """
    with _COPY_LOCK:
        snapshot = _clipboard_snapshot()
        if snapshot is None:
            return None

        keep_restore = True
        try:
            _release_modifiers()
            time.sleep(settle_before)
            seq_before = clipboard_sequence()
            try:
                _send_copy()
            except Exception as exc:  # noqa: BLE001
                log.debug("预检测复制失败：%s", exc)
                return None

            seq_after = None
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                time.sleep(poll_interval)
                if clipboard_sequence() != seq_before:
                    seq_after = clipboard_sequence()
                    break
            if seq_after is None:
                return None

            candidate = get_clipboard_text()
            if candidate is None:
                for _ in range(6):
                    time.sleep(0.02)
                    candidate = get_clipboard_text()
                    if candidate is not None:
                        break
            if candidate is None or not candidate.strip():
                return None
            if clipboard_sequence() != seq_after:
                keep_restore = False
                return None
            result = candidate.strip()
            if _looks_like_password_mask(result):
                return None
            return result
        finally:
            if keep_restore:
                _restore_clipboard(snapshot)


def _restore(saved_text: str | None, had_text: bool) -> None:
    if not had_text:
        return  # 原来不是文本，没有可还原的东西
    try:
        set_clipboard_text(saved_text or "")
    except Exception:  # noqa: BLE001
        log.warning("剪贴板还原失败，不影响翻译结果")


def copy_to_clipboard(text: str) -> bool:
    """供"把译文写回剪贴板"功能使用。"""
    return set_clipboard_text(text)
