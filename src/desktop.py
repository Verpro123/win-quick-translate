# -*- coding: utf-8 -*-
"""桌面几何与鼠标划词监视。

为什么不用 tkinter 的屏幕尺寸
----------------------------
`winfo_screenwidth()` 只认主显示器：接了副屏之后，所有位置计算都会被
硬夹回主屏范围内，气泡自然永远出不去。这里统一改用 Windows 的显示器
API（MonitorFromPoint / GetMonitorInfo），按鼠标所在的那块屏算工作区。

抓屏则按"整个虚拟屏幕"拍，再按虚拟屏幕原点裁剪 —— 副屏在主屏左边时
坐标是负的，直接拿 tkinter 坐标乘以缩放会切错位置。

划词检测用的是低频轮询而不是全局鼠标钩子：
    WH_MOUSE_LL 钩子回调一旦变慢，整台机器的鼠标都会跟着卡；
    而这里只需要知道"松开左键时，刚才是不是划过一段"，20ms 轮询足够。
"""

from __future__ import annotations

import ctypes
import logging
import threading
import time
from ctypes import wintypes

log = logging.getLogger(__name__)

user32 = ctypes.WinDLL("user32", use_last_error=True)

SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79

MONITOR_DEFAULTTONEAREST = 2
GA_PARENT = 1
GA_ROOT = 2
VK_LBUTTON = 0x01
WM_NCHITTEST = 0x0084
HTCLIENT = 1
SMTO_ABORTIFHUNG = 0x0002

# 桌面、任务栏这些地方拖动不是选字，别弹翻译按钮
_SKIP_CLASSES = {
    "Progman",
    "WorkerW",
    "Shell_TrayWnd",
    "Shell_SecondaryTrayWnd",
    "TrayNotifyWnd",
    "DV2ControlHost",
    "SysListView32",
}

# 这些控件自己就是列表/图标/工具栏，拖动它们是框选或排序，不是选文字
_NON_TEXT_LEAVES = {
    "ListBox",
    "Listbox",
    "ComboBox",
    "SysListView32",
    "SysTreeView32",
    "SysHeader32",
    "SysTabControl32",
    "SysMonthCal32",
    "SysDateTimePick32",
    "ToolbarWindow32",
    "ReBarWindow32",
    "MSTaskListWClass",
    "DV2ControlHost",
    "NetUIHWND",
    "SHELLDLL_DefView",
    "msctls_trackbar32",
    "msctls_updown32",
    "msctls_statusbar32",
}

# 资源管理器/桌面外壳的视图容器。最内层往往还是 DirectUIHWND，
# 光看叶子类名分不出来，必须看这一层父控件。
_SHELL_VIEW_CLASSES = {
    "SHELLDLL_DefView",
    "DUIViewWndClassName",
    "NamespaceTreeControl",
}


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


class MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", RECT),
        ("rcWork", RECT),
        ("dwFlags", wintypes.DWORD),
    ]


user32.GetCursorPos.argtypes = [ctypes.POINTER(POINT)]
user32.GetCursorPos.restype = wintypes.BOOL

user32.MonitorFromPoint.argtypes = [POINT, wintypes.DWORD]
user32.MonitorFromPoint.restype = wintypes.HANDLE

user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MONITORINFO)]
user32.GetMonitorInfoW.restype = wintypes.BOOL

user32.WindowFromPoint.argtypes = [POINT]
user32.WindowFromPoint.restype = wintypes.HWND

user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetAncestor.restype = wintypes.HWND

user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.restype = ctypes.c_int

user32.SendMessageTimeoutW.argtypes = [
    wintypes.HWND,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
    wintypes.UINT,
    wintypes.UINT,
    ctypes.POINTER(ctypes.c_size_t),
]
user32.SendMessageTimeoutW.restype = ctypes.c_size_t

user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short


# ---------------------------------------------------------------- 屏幕几何


def cursor_pos() -> tuple[int, int]:
    point = POINT()
    if not user32.GetCursorPos(ctypes.byref(point)):
        return 0, 0
    return int(point.x), int(point.y)


def monitor_work_area(x: int, y: int) -> tuple[int, int, int, int]:
    """返回 (left, top, right, bottom)：包含该点的显示器可用区域。

    已经扣掉了任务栏，所以气泡定位时不用再留任务栏的余量。
    """
    monitor = user32.MonitorFromPoint(POINT(x, y), MONITOR_DEFAULTTONEAREST)
    info = MONITORINFO()
    info.cbSize = ctypes.sizeof(MONITORINFO)
    if monitor and user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
        work = info.rcWork
        return int(work.left), int(work.top), int(work.right), int(work.bottom)

    return (
        0,
        0,
        int(user32.GetSystemMetrics(0)),
        int(user32.GetSystemMetrics(1)),
    )


def virtual_screen() -> tuple[int, int, int, int]:
    """整个虚拟屏幕的 (left, top, width, height)，副屏在左边时 left 为负。"""
    return (
        int(user32.GetSystemMetrics(SM_XVIRTUALSCREEN)),
        int(user32.GetSystemMetrics(SM_YVIRTUALSCREEN)),
        int(user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)),
        int(user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)),
    )


def clamp_to_work_area(
    x: int, y: int, width: int, height: int, margin: int = 8
) -> tuple[int, int]:
    """把窗口夹进它中心点所在显示器的工作区里。"""
    left, top, right, bottom = monitor_work_area(x + width // 2, y + height // 2)
    max_x = right - width - margin
    max_y = bottom - height - margin
    x = max(left + margin, min(x, max_x))
    y = max(top + margin, min(y, max_y))
    return x, y


def _class_name(hwnd: int) -> str:
    buffer = ctypes.create_unicode_buffer(256)
    length = user32.GetClassNameW(hwnd, buffer, len(buffer))
    return buffer.value if length else ""


def window_at(x: int, y: int) -> int:
    """屏幕坐标处窗口的顶层句柄；那里什么都没有就返回 0。"""
    hwnd = user32.WindowFromPoint(POINT(x, y))
    if not hwnd:
        return 0
    return int(user32.GetAncestor(hwnd, GA_ROOT) or hwnd)


def window_chain(x: int, y: int) -> list[str]:
    """该点处从最内层控件到顶层窗口的类名列表。"""
    chain: list[str] = []
    current = user32.WindowFromPoint(POINT(x, y))
    while current:
        chain.append(_class_name(current))
        parent = user32.GetAncestor(current, GA_PARENT)
        if not parent or parent == current:
            break
        current = parent
    return chain


def chain_is_text_surface(chain: list[str]) -> bool:
    """只看控件链，判断是不是"可以选文字"的地方（不碰系统 API，方便自检）。"""
    if not chain:
        return False
    leaf, root = chain[0], chain[-1]
    if leaf in _NON_TEXT_LEAVES or root in _SKIP_CLASSES:
        return False
    # 资源管理器的文件区/导航树、桌面外壳视图：最内层往往还是 DirectUIHWND，
    # 只有看父控件这一层才能和真正的文本框区分开
    return not any(name in _SHELL_VIEW_CLASSES for name in chain)


def point_is_text_surface(x: int, y: int) -> bool:
    """判断该点是否像"可以选文字"的地方。

    先排除资源管理器的文件区（SHELLDLL_DefView）、导航树、任务栏、桌面等
    拖框/拖图标的地方 —— 在这些地方按住左键划一下会选中多个文件/文件夹，
    但不是选文字，不能弹翻译按钮。
    再用 WM_NCHITTEST 区分客户区与标题栏 / 边框 / 滚动条。
    """
    try:
        if not chain_is_text_surface(window_chain(x, y)):
            return False

        hwnd = user32.WindowFromPoint(POINT(x, y))
        if not hwnd:
            return False
        target = user32.GetAncestor(hwnd, GA_ROOT) or hwnd

        packed = ((y & 0xFFFF) << 16) | (x & 0xFFFF)
        result = ctypes.c_size_t(0)
        ok = user32.SendMessageTimeoutW(
            target, WM_NCHITTEST, 0, packed,
            SMTO_ABORTIFHUNG, 60, ctypes.byref(result),
        )
        if not ok:
            return False
        return int(result.value) == HTCLIENT
    except Exception as exc:  # noqa: BLE001
        log.debug("命中测试失败（%s），按可划选处理", exc)
        return True


# ---------------------------------------------------------------- 划词监视


def _distance(a: tuple[int, int], b: tuple[int, int]) -> float:
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5


class SelectionWatcher:
    """后台轮询左键，发现"拖选"或"双击选词"就回调。

    回调运行在这个线程里，只允许丢队列，不能碰界面。
    on_click 在按下时触发，用来收起上一轮留下的翻译按钮。
    """

    DRAG_MIN = 6          # 按住后移动超过这么多像素才算划过
    DOUBLE_CLICK = 0.45   # 两次点击间隔上限（秒）
    POLL = 0.02

    def __init__(self, on_select, on_click=None) -> None:
        self._on_select = on_select
        self._on_click = on_click
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="mousewatch", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _notify(self, callback, *args) -> None:
        if callback is None:
            return
        try:
            callback(*args)
        except Exception:  # noqa: BLE001 - 监视线程不能因为回调出错就死掉
            log.exception("鼠标监视回调出错")

    def _loop(self) -> None:
        pressed = False
        press_pos = (0, 0)
        press_hwnd = 0
        last_click_at = 0.0
        last_click_pos = (0, 0)
        is_double = False

        while not self._stop.wait(self.POLL):
            try:
                down = bool(user32.GetAsyncKeyState(VK_LBUTTON) & 0x8000)
            except Exception:  # noqa: BLE001
                continue

            if down and not pressed:
                pressed = True
                press_pos = cursor_pos()
                press_hwnd = window_at(*press_pos)
                now = time.monotonic()
                is_double = (
                    now - last_click_at <= self.DOUBLE_CLICK
                    and _distance(press_pos, last_click_pos) <= 6
                )
                last_click_at = now
                last_click_pos = press_pos
                self._notify(self._on_click, *press_pos)

            elif not down and pressed:
                pressed = False
                release = cursor_pos()
                dragged = _distance(release, press_pos) >= self.DRAG_MIN
                if not (dragged or is_double):
                    continue
                # 起点和终点都得像文字区：从资源管理器文件区拖到别处也不能弹
                if not point_is_text_surface(*press_pos):
                    continue
                if not point_is_text_surface(*release):
                    continue
                self._notify(self._on_select, release[0], release[1], press_hwnd)
