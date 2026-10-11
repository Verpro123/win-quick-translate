# -*- coding: utf-8 -*-
"""窗口背景效果：Windows 亚克力毛玻璃 + 圆角。

为什么单独抽一个模块
--------------------
毛玻璃靠的是 DWM 的合成能力，而不是我们画出来的。要让模糊透出来，
窗口自身那层背景必须是"透明"的 —— 可 tkinter 的控件又强制要有背景色。
这里把各种尝试封装起来，用哪种由实测结果决定（见 ui.py 里的选择）。

用到的两个 API 都没进官方文档（SetWindowCompositionAttribute），
所以每一步都做了防御：拿不到句柄、调用失败、系统版本不支持，
都只是安静地退回上一种效果，绝不让气泡开不出来。
"""

from __future__ import annotations

import ctypes
import logging
from ctypes import wintypes

log = logging.getLogger(__name__)

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)

GA_ROOT = 2

user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetAncestor.restype = wintypes.HWND

# ------------------------------------------------------------ 亚克力

WCA_ACCENT_POLICY = 19

ACCENT_DISABLED = 0
ACCENT_ENABLE_BLURBEHIND = 3            # 普通模糊（Win10 1803 之前的老效果）
ACCENT_ENABLE_ACRYLICBLURBEHIND = 4     # 亚克力：模糊 + 噪点 + 染色

# SetWindowCompositionAttribute 未进文档，但不影响它在 user32 里被导出
try:
    _SetWindowCompositionAttribute = user32.SetWindowCompositionAttribute
    _SetWindowCompositionAttribute.argtypes = [wintypes.HWND, ctypes.c_void_p]
    _SetWindowCompositionAttribute.restype = wintypes.BOOL
    HAS_COMPOSITION_API = True
except AttributeError:  # pragma: no cover
    _SetWindowCompositionAttribute = None
    HAS_COMPOSITION_API = False
    log.warning("系统未导出 SetWindowCompositionAttribute，毛玻璃不可用")


class ACCENTPOLICY(ctypes.Structure):
    _fields_ = [
        ("AccentState", ctypes.c_int),
        ("AccentFlags", ctypes.c_int),
        ("GradientColor", ctypes.c_uint),   # 注意是 ABGR，不是 RGB
        ("AnimationId", ctypes.c_int),
    ]


class WINCOMPATTRDATA(ctypes.Structure):
    _fields_ = [
        ("Attribute", ctypes.c_int),
        ("Data", ctypes.c_void_p),
        ("SizeOfData", ctypes.c_size_t),
    ]


def to_abgr(hex_color: str, alpha: int) -> int:
    """把 #RRGGBB 转成 API 要的 ABGR 整数。"""
    text = hex_color.lstrip("#")
    if len(text) != 6:
        text = "1F2023"
    red = int(text[0:2], 16)
    green = int(text[2:4], 16)
    blue = int(text[4:6], 16)
    return (alpha & 0xFF) << 24 | (blue << 16) | (green << 8) | red


def hwnd_of(widget) -> int:
    """拿到控件所属顶层窗口的真实句柄。"""
    widget.update_idletasks()
    raw = widget.winfo_id()
    return int(user32.GetAncestor(wintypes.HWND(raw), GA_ROOT) or raw)


def apply_acrylic(hwnd: int, tint_hex: str = "#1F2023", alpha: int = 0xB4) -> bool:
    """给窗口上亚克力毛玻璃。tint 会被 DWM 叠在模糊层上。"""
    if not HAS_COMPOSITION_API:
        return False

    accent = ACCENTPOLICY()
    accent.AccentState = ACCENT_ENABLE_ACRYLICBLURBEHIND
    # AccentFlags=2 是让边框和标题栏一起参与，气泡没有边框，给 0 更干净
    accent.AccentFlags = 0
    accent.GradientColor = to_abgr(tint_hex, alpha)
    accent.AnimationId = 0

    data = WINCOMPATTRDATA()
    data.Attribute = WCA_ACCENT_POLICY
    data.Data = ctypes.cast(ctypes.pointer(accent), ctypes.c_void_p)
    data.SizeOfData = ctypes.sizeof(accent)

    try:
        ok = _SetWindowCompositionAttribute(
            wintypes.HWND(hwnd), ctypes.byref(data)
        )
        return bool(ok)
    except Exception as exc:  # noqa: BLE001
        log.warning("设置亚克力失败：%s", exc)
        return False


def apply_blur(hwnd: int, tint_hex: str = "#1F2023", alpha: int = 0xB4) -> bool:
    """退一档：老式模糊（某些机器上比亚克力更稳）。"""
    if not HAS_COMPOSITION_API:
        return False

    accent = ACCENTPOLICY()
    accent.AccentState = ACCENT_ENABLE_BLURBEHIND
    accent.AccentFlags = 0
    accent.GradientColor = to_abgr(tint_hex, alpha)
    accent.AnimationId = 0

    data = WINCOMPATTRDATA()
    data.Attribute = WCA_ACCENT_POLICY
    data.Data = ctypes.cast(ctypes.pointer(accent), ctypes.c_void_p)
    data.SizeOfData = ctypes.sizeof(accent)

    try:
        return bool(_SetWindowCompositionAttribute(wintypes.HWND(hwnd), ctypes.byref(data)))
    except Exception:  # noqa: BLE001
        return False


def clear_backdrop(hwnd: int) -> None:
    """撤掉毛玻璃，恢复成普通窗口。"""
    if not HAS_COMPOSITION_API:
        return
    accent = ACCENTPOLICY()
    accent.AccentState = ACCENT_DISABLED
    accent.AccentFlags = 0
    accent.GradientColor = 0
    accent.AnimationId = 0
    data = WINCOMPATTRDATA()
    data.Attribute = WCA_ACCENT_POLICY
    data.Data = ctypes.cast(ctypes.pointer(accent), ctypes.c_void_p)
    data.SizeOfData = ctypes.sizeof(accent)
    try:
        _SetWindowCompositionAttribute(wintypes.HWND(hwnd), ctypes.byref(data))
    except Exception:  # noqa: BLE001
        pass


# ------------------------------------------------------------ 圆角

DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWCP_ROUND = 2
DWMWCP_DONOTROUND = 1


def apply_dwm_round(hwnd: int, enable: bool = True) -> bool:
    """Win11 的原生圆角。对无边框窗口不一定生效，失败也没关系。"""
    try:
        value = ctypes.c_int(DWMWCP_ROUND if enable else DWMWCP_DONOTROUND)
        result = dwmapi.DwmSetWindowAttribute(
            wintypes.HWND(hwnd),
            ctypes.c_uint(DWMWA_WINDOW_CORNER_PREFERENCE),
            ctypes.byref(value),
            ctypes.sizeof(value),
        )
        return result == 0
    except Exception:  # noqa: BLE001
        return False


gdi32.CreateRoundRectRgn.argtypes = [
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int
]
gdi32.CreateRoundRectRgn.restype = wintypes.HRGN
user32.SetWindowRgn.argtypes = [wintypes.HWND, wintypes.HRGN, wintypes.BOOL]
user32.SetWindowRgn.restype = ctypes.c_int


def apply_round_region(hwnd: int, width: int, height: int, radius: int) -> bool:
    """用 GDI 区域把窗口裁成圆角。

    这是"兜底圆角"：不像 DWM 圆角那样有抗锯齿，但**对任何窗口都有效**，
    包括 overrideredirect 的无边框窗口。尺寸每次变化都要重设。

    注意：SetWindowRgn 成功后这块区域的归属权就交给系统了，不能再手动释放。
    """
    try:
        region = gdi32.CreateRoundRectRgn(
            0, 0, width + 1, height + 1, radius * 2, radius * 2
        )
        if not region:
            return False
        return bool(user32.SetWindowRgn(wintypes.HWND(hwnd), region, True))
    except Exception:  # noqa: BLE001
        return False


def apply_layered_alpha(widget, alpha: float) -> bool:
    """整体透明度（0~1）。这不是真模糊，但能让卡片轻盈些。"""
    try:
        widget.attributes("-alpha", alpha)
        return True
    except Exception:  # noqa: BLE001
        return False
