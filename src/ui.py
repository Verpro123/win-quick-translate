# -*- coding: utf-8 -*-
"""界面：鼠标旁的划词气泡、划词「翻译」按钮 + 设置窗口。

背景为什么是自己画的
--------------------
Windows 的亚克力（SetWindowCompositionAttribute）和 tkinter 做圆角所必需的
键控透明（-transparentcolor）会抢同一套 layered window 属性，实测下来后者
会把前者覆盖掉，模糊根本出不来。

所以改成自己造：气泡弹出前先"拍"一张它背后那个位置的屏幕，做高斯模糊、
压暗染色、切圆角，再当作卡片背景贴上去。好处是效果是真的模糊，且不依赖
任何未文档化的系统 API，Windows 10/11 表现一致。

关键约束：气泡必须**永远不抢输入焦点**，否则用户按热键时焦点被气泡夺走，
Ctrl+C 就发给了气泡本身，取词直接失败 —— 所以窗口创建时打上 WS_EX_NOACTIVATE。
"""

from __future__ import annotations

import ctypes
import logging
import threading
import tkinter as tk
from ctypes import wintypes
from pathlib import Path
from tkinter import filedialog
from tkinter import font as tkfont
from tkinter import ttk

import keyboard
from PIL import Image, ImageDraw, ImageFilter, ImageGrab, ImageTk

import backgrounds
import config as config_mod
import desktop
import system
from engines import TranslationResult, build_engine

log = logging.getLogger(__name__)

FONT_CN = "Microsoft YaHei UI"
TRANSPARENT_KEY = "#FE01FE"  # 给圆角用的键控透明色，日常内容不会撞上

# 深色玻璃 / 浅色玻璃两套配色
GLASS_DARK = {
    "tint": "#111216",
    "tint_opacity": 0.84,
    "border": "#3C4048",
    "divider": "#3A3E45",
    "dashed": "#373B42",
    "source": "#8E949E",
    "body": "#ECEEF1",
    "phonetic": "#79B8FF",
    "example": "#B9BFC8",
    "note": "#959BA6",
    "quote": "#4B5058",
    "label": "#787E88",
    "error": "#FF8A80",
}
GLASS_LIGHT = {
    "tint": "#F6F7F9",
    "tint_opacity": 0.84,
    "border": "#D5D9DF",
    "divider": "#E3E6EA",
    "dashed": "#DCE0E5",
    "source": "#767B84",
    "body": "#1B1D21",
    "phonetic": "#1A73E8",
    "example": "#4A5058",
    "note": "#7A8089",
    "quote": "#C5CAD1",
    "label": "#878D96",
    "error": "#D93025",
}

# ------------------------------------------------------------ 窗口样式工具

_user32 = ctypes.WinDLL("user32", use_last_error=True)

GWL_EXSTYLE = -20
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080
GA_ROOT = 2
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
WDA_NONE = 0x00000000
WDA_EXCLUDEFROMCAPTURE = 0x00000011   # Win10 2004+：屏幕上照常显示，截屏里不出现

if ctypes.sizeof(ctypes.c_void_p) == 8:
    _GetWindowLong = _user32.GetWindowLongPtrW
    _SetWindowLong = _user32.SetWindowLongPtrW
    _LONG_PTR = ctypes.c_longlong
else:  # pragma: no cover - 32 位系统
    _GetWindowLong = _user32.GetWindowLongW
    _SetWindowLong = _user32.SetWindowLongW
    _LONG_PTR = ctypes.c_long

_GetWindowLong.argtypes = [wintypes.HWND, ctypes.c_int]
_GetWindowLong.restype = _LONG_PTR
_SetWindowLong.argtypes = [wintypes.HWND, ctypes.c_int, _LONG_PTR]
_SetWindowLong.restype = _LONG_PTR
_user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
_user32.GetAncestor.restype = wintypes.HWND
_user32.SetWindowPos.argtypes = [
    wintypes.HWND, wintypes.HWND,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint,
]
_user32.SetWindowPos.restype = wintypes.BOOL
_user32.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
_user32.SetWindowDisplayAffinity.restype = wintypes.BOOL


def _root_hwnd(widget: tk.Misc) -> int:
    raw = widget.winfo_id()
    return int(_user32.GetAncestor(wintypes.HWND(raw), GA_ROOT) or raw)


def _hide_from_capture(widget: tk.Misc) -> bool:
    """让窗口不出现在截屏里，但屏幕上照常显示。

    截背景时必须别拍到气泡自己。以前的做法是先 withdraw 再 deiconify，
    窗口会消失几十毫秒，肉眼就是"闪一下"。换成截屏排除后，窗口一直留在
    屏幕上，毛玻璃更新完直接替换，没有闪烁。
    """
    try:
        return bool(
            _user32.SetWindowDisplayAffinity(
                wintypes.HWND(_root_hwnd(widget)), WDA_EXCLUDEFROMCAPTURE
            )
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("截屏排除不可用（%s），退回隐藏窗口", exc)
        return False


def _show_in_capture(widget: tk.Misc) -> None:
    try:
        _user32.SetWindowDisplayAffinity(wintypes.HWND(_root_hwnd(widget)), WDA_NONE)
    except Exception:  # noqa: BLE001
        pass


def place_window(widget: tk.Misc, x: int, y: int, width: int, height: int) -> None:
    """用 Win32 直接摆窗口。

    不能用 wm geometry：tk 的几何串里 "-1182" 表示「距屏幕右边缘 1182」，
    而不是负坐标 —— 副屏在主屏左边时，气泡永远摆不过去。
    """
    _user32.SetWindowPos(
        wintypes.HWND(_root_hwnd(widget)), None,
        x, y, width, height,
        SWP_NOZORDER | SWP_NOACTIVATE,
    )


def _apply_no_activate(widget: tk.Misc) -> None:
    """给窗口打上「不激活」「不进 Alt+Tab」标记。"""
    try:
        widget.update_idletasks()
        root_hwnd = _root_hwnd(widget)
        current = _GetWindowLong(wintypes.HWND(root_hwnd), GWL_EXSTYLE)
        _SetWindowLong(
            wintypes.HWND(root_hwnd),
            GWL_EXSTYLE,
            current | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW,
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("设置窗口扩展样式失败（不影响主功能）：%s", exc)


def _in_rect(x: int, y: int, rect: tuple[int, int, int, int]) -> bool:
    rx, ry, rw, rh = rect
    return rx <= x < rx + rw and ry <= y < ry + rh


def _rounded_rect(canvas: tk.Canvas, x1, y1, x2, y2, radius, **kwargs):
    """用平滑多边形画圆角矩形 —— tkinter 没有原生圆角，这是通用做法。"""
    points = [
        x1 + radius, y1,
        x2 - radius, y1,
        x2, y1,
        x2, y1 + radius,
        x2, y2 - radius,
        x2, y2,
        x2 - radius, y2,
        x1 + radius, y2,
        x1, y2,
        x1, y2 - radius,
        x1, y1 + radius,
        x1, y1,
    ]
    return canvas.create_polygon(points, smooth=True, **kwargs)


def system_is_dark() -> bool:
    """读注册表判断系统是否为深色主题，判不出来就按深色走。"""
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        ) as key:
            value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
            return int(value) == 0
    except Exception:  # noqa: BLE001
        return True


# ------------------------------------------------------------ 气泡窗


class BubbleWindow:
    """鼠标旁的小气泡：毛玻璃底 + 原文 + 音标 + 释义 + 例句 + 注释。"""

    PAD_X = 18
    PAD_Y = 16
    RADIUS = 12

    GAP_AFTER_LINE = 10
    GAP_LINE = 3
    QUOTE_INDENT = 16

    # 长内容：超过屏幕就限高 + 滚轮翻页；右下角可以拖大小
    MIN_CARD_W = 200
    MIN_VIEW_H = 60
    MAX_CARD_W = 1200
    SCROLL_STEP = 64          # 滚轮一格滚动的像素
    SCROLLBAR_W = 6
    GRIP = 16                 # 右下角缩放手柄
    EDGE = 5                  # 右边/下边拖拽缩放的命中宽度
    LIVE_PREVIEW_DOWNSCALE = 2   # 缩放预览把底图先缩小这么多倍再模糊，省算力

    def __init__(self, root: tk.Tk, cfg: dict) -> None:
        self.root = root
        self.cfg = cfg
        self._win: tk.Toplevel | None = None
        self._canvas: tk.Canvas | None = None
        self._job: str | None = None
        self._photo: ImageTk.PhotoImage | None = None   # 必须留引用，否则被回收
        self._photo_key: tuple | None = None
        self._font_cache: dict[tuple, tkfont.Font] = {}
        # 拖动状态
        self._dragging = False
        self._dragged = False
        self._drag_start: tuple[int, int] | None = None
        self._drag_offset: tuple[int, int] | None = None
        # 自己记住窗口位置和尺寸：Win32 摆的窗口，tk 的 winfo 可能慢一拍
        self._pos: tuple[int, int] | None = None
        self._size: tuple[int, int] | None = None
        # 最近一次的内容，尺寸变化时要用它重新排版
        self._last: tuple[str, TranslationResult, bool] | None = None
        self._colors: dict | None = None
        self._painter = None
        self._content_w = 0
        self._full_h = 0        # 内容总高度
        self._view_h = 0        # 当前可见高度
        self._scroll = 0        # 已向上滚动的像素
        self._scrollable = False
        # 用户拖出来的尺寸，本次运行内一直沿用
        self._width_override: int | None = None
        self._height_override: int | None = None
        # 滚轮条 / 缩放拖拽状态
        self._scroll_drag = False
        self._resizing = False
        self._resize_origin: tuple[int, int, int, int, bool, bool] | None = None
        self._resize_job: str | None = None
        self._pending_size: tuple[int, int] | None = None
        self._cursor = "fleur"
        self._luminance_cache: dict[str, float] = {}
        # 缩放时的背景预览：开局抓一张整屏快照，之后每帧只从里面裁
        self._drag_snapshot: Image.Image | None = None
        self._drag_meta: tuple | None = None
        self._live_attempted = False

    # ---- 窗口生命周期 ----

    def _ensure_window(self) -> tk.Toplevel:
        if self._win is not None and self._win.winfo_exists():
            return self._win

        win = tk.Toplevel(self.root)
        win.overrideredirect(True)          # 去掉标题栏和边框
        win.attributes("-topmost", True)    # 始终压在最上层
        win.configure(bg=TRANSPARENT_KEY, cursor="fleur")
        try:
            win.attributes("-transparentcolor", TRANSPARENT_KEY)
        except tk.TclError:
            log.debug("当前环境不支持键控透明，圆角会退化为直角")
        win.withdraw()

        canvas = tk.Canvas(win, bg=TRANSPARENT_KEY, highlightthickness=0, bd=0)
        canvas.pack(fill="both", expand=True)

        _apply_no_activate(win)

        win.bind("<ButtonPress-1>", self._on_press)
        win.bind("<B1-Motion>", self._on_motion)
        win.bind("<ButtonRelease-1>", self._on_release)
        win.bind("<Enter>", lambda _e: self._cancel_timer())
        win.bind("<Leave>", lambda _e: self._arm_timer())
        win.bind("<Motion>", self._on_hover)
        # 窗口是 WS_EX_NOACTIVATE，但 Win10 1703 起滚轮按"指针下的窗口"投递，
        # 悬停即可滚动；X11 的 Button-4/5 一并兼容。
        win.bind("<MouseWheel>", self._on_wheel)
        canvas.bind("<MouseWheel>", self._on_wheel)
        win.bind("<Button-4>", lambda event: self._on_wheel(event, 1))
        win.bind("<Button-5>", lambda event: self._on_wheel(event, -1))

        self._win = win
        self._canvas = canvas
        return win

    def hide(self) -> None:
        self._cancel_timer()
        if self._resize_job is not None:
            try:
                self.root.after_cancel(self._resize_job)
            except (ValueError, tk.TclError):
                pass
            self._resize_job = None
        self._dragging = False
        self._resizing = False
        self._scroll_drag = False
        self._resize_origin = None
        self._drag_start = None
        self._drag_offset = None
        self._clear_drag_snapshot()
        if self._win is not None and self._win.winfo_exists():
            self._win.withdraw()

    # ---- 对外入口 ----

    def show(
        self,
        source: str,
        result: TranslationResult | str,
        is_error: bool = False,
        auto_hide: bool = True,
    ) -> None:
        if isinstance(result, str):
            result = TranslationResult(raw=result)

        self._last = (source, result, is_error)
        colors = self._pick_palette()
        self._colors = colors
        self._render(colors, reposition=not self._dragging, reset_scroll=True)

        if auto_hide:
            self._arm_timer()
        else:
            self._cancel_timer()

    def _pick_palette(self) -> dict:
        """渐变/图片是花哨底图，按底图亮度挑字色；其余跟随系统深浅色。"""
        mode = self._backdrop_mode()
        if mode in {"gradient", "image"}:
            luminance = self._base_luminance(mode)
            if luminance is not None:
                return GLASS_DARK if luminance < 155 else GLASS_LIGHT
        return GLASS_DARK if system_is_dark() else GLASS_LIGHT

    def _base_luminance(self, mode: str) -> float | None:
        if mode == "gradient":
            return backgrounds.preset_luminance(
                str(self.cfg.get("backdrop_gradient", ""))
            )
        path = self._image_path()
        if not path:
            return None
        cached = self._luminance_cache.get(path)
        if cached is not None:
            return cached
        value = backgrounds.image_luminance(path)
        if value is not None:
            self._luminance_cache[path] = value
        return value

    def _render(
        self,
        colors: dict,
        reposition: bool = True,
        reset_scroll: bool = False,
        preview: bool = False,
    ) -> None:
        """排版 → 定位 → 铺背景 → 铺内容。新译文、拖动结束、缩放都走这里。"""
        win = self._ensure_window()
        canvas = self._canvas
        assert canvas is not None
        if self._last is None:
            return

        source, result, is_error = self._last
        font_size = int(self.cfg.get("font_size", 11))

        max_width = self._width_override or int(self.cfg.get("bubble_width", 520))
        content_w, full_h, painter = self._layout(
            source, result, is_error, colors, font_size,
            max_width=max_width, force_width=self._width_override,
        )
        card_w = max(self.MIN_CARD_W, content_w + self.PAD_X * 2)

        # 高度封顶：内容再长也不越过当前屏幕的工作区，剩下的交给滚轮
        max_h = self._max_view_height()
        if self._height_override is not None:
            view_h = min(full_h, max(self.MIN_VIEW_H, min(self._height_override, max_h)))
        else:
            view_h = min(full_h, max_h)
        view_h = max(1, int(view_h))

        if reset_scroll:
            self._scroll = 0
        max_scroll = max(0, full_h - view_h)
        self._scroll = max(0, min(self._scroll, max_scroll))
        self._scrollable = max_scroll > 0

        self._content_w = content_w
        self._full_h = full_h
        self._view_h = view_h
        self._painter = painter

        # 定位：新译文贴着鼠标弹；拖动/缩放中则留在原地。
        if reposition and not self._dragging:
            self._dragging = False
            self._dragged = False
            self._drag_start = None
            self._drag_offset = None
            x, y = self._place(card_w, view_h)
        elif self._pos is None:
            x, y = self._place(card_w, view_h)
        else:
            x, y = desktop.clamp_to_work_area(self._pos[0], self._pos[1], card_w, view_h)

        canvas.configure(width=card_w, height=view_h)
        image = self._backdrop_image(
            x, y, card_w, view_h, colors, win, live=preview
        )
        canvas.delete("all")
        self._draw_backdrop(canvas, card_w, view_h, colors, image)
        painter()
        if self._scroll:
            canvas.move("content", 0, -self._scroll)
        self._draw_scrollbar(card_w, view_h, colors)

        self._pos = (x, y)
        self._size = (card_w, view_h)
        place_window(win, x, y, card_w, view_h)
        win.deiconify()
        win.lift()

    def _max_view_height(self) -> int:
        """当前屏幕工作区能容纳的气泡最大高度，留一点上下呼吸空间。"""
        _left, top, _right, bottom = desktop.monitor_work_area(*desktop.cursor_pos())
        return max(self.MIN_VIEW_H, bottom - top - 28)

    # ---- 背景：自绘毛玻璃 ----

    def _backdrop_image(
        self, x: int, y: int, width: int, height: int, colors: dict,
        win: tk.Toplevel, live: bool = False,
    ) -> ImageTk.PhotoImage | None:
        """取（或复用）背景底图；纯色模式或生成失败时返回 None。

        live=True 是拖动 / 缩放中的预览帧：不抓屏，从快照里裁，也不进缓存 ——
        预览帧的尺寸或位置每帧都在变，缓存没有意义。
        """
        mode = self._backdrop_mode()
        if mode == "solid":
            return None
        if live:
            card = self._compose_backdrop(
                mode, x, y, width, height, colors, win, live=True
            )
            if card is None:
                return None
            photo = ImageTk.PhotoImage(card)
            self._photo = photo
            self._photo_key = None      # 标记成预览图：之后必须全量重算
            return photo
        key = self._backdrop_key(mode, x, y, width, height, colors)
        if self._photo is not None and self._photo_key == key:
            return self._photo
        card = self._compose_backdrop(mode, x, y, width, height, colors, win)
        if card is None:
            return None
        photo = ImageTk.PhotoImage(card)
        self._photo = photo
        self._photo_key = key
        return photo

    def _backdrop_mode(self) -> str:
        mode = str(self.cfg.get("backdrop", "blur")).lower()
        return mode if mode in {"blur", "gradient", "image", "solid"} else "blur"

    def _backdrop_key(
        self, mode: str, x: int, y: int, width: int, height: int, colors: dict
    ) -> tuple:
        key: tuple = (mode, width, height, colors.get("tint"))
        if mode == "blur":
            key += (x, y)                 # 毛玻璃取样位置一变就得重拍
        elif mode == "gradient":
            key += (str(self.cfg.get("backdrop_gradient", "")),)
        elif mode == "image":
            key += (
                self._image_path(),
                round(self._image_blur(), 2),
                round(self._dim_opacity(colors), 3),
            )
        return key

    def _image_blur(self) -> float:
        try:
            value = float(self.cfg.get("backdrop_image_blur", 0) or 0)
        except (TypeError, ValueError):
            value = 0.0
        return max(0.0, min(60.0, value))

    def _image_path(self) -> str:
        """自定义图片路径；相对路径按配置文件所在目录解析。"""
        raw = str(self.cfg.get("backdrop_image", "") or "").strip()
        if not raw:
            return ""
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = config_mod.config_path().parent / path
        return str(path)

    def _compose_backdrop(
        self, mode: str, x: int, y: int, width: int, height: int,
        colors: dict, win: tk.Toplevel, live: bool = False,
    ) -> Image.Image | None:
        """按模式生成卡片底图（含圆角与描边）。失败返回 None，调用方退回纯色。

        live=True 是拖动中的预览帧：底图从拖动开局那张快照里裁，不再抓屏。
        """
        try:
            if live:
                base = self._base_from_snapshot(mode, x, y, width, height)
                if base is None and mode == "gradient":
                    # 快照都拿不到时至少给渐变色，别让渐变主题掉成纯色
                    base = backgrounds.gradient(
                        (width, height), str(self.cfg.get("backdrop_gradient", ""))
                    )
            elif mode == "blur":
                snapshot = self._grab_behind(x, y, width, height, win)
                base = (
                    None if snapshot is None
                    else snapshot.filter(ImageFilter.GaussianBlur(11))
                )
            elif mode == "gradient":
                base = backgrounds.gradient(
                    (width, height), str(self.cfg.get("backdrop_gradient", ""))
                )
                # 渐变底下再掺一点真实画面，才像"毛玻璃渐变"而不是一张色纸
                snapshot = self._grab_behind(x, y, width, height, win)
                if snapshot is not None:
                    frosted = snapshot.filter(ImageFilter.GaussianBlur(18))
                    base = Image.blend(base, frosted, 0.3)
            elif mode == "image":
                base = backgrounds.load_image(
                    self._image_path(), (width, height), self._image_blur()
                )
                if base is None:
                    # 图片没配或读不出来时退回渐变，别让气泡变成纯黑
                    base = backgrounds.gradient(
                        (width, height), str(self.cfg.get("backdrop_gradient", ""))
                    )
            else:
                return None

            if base is None:
                return None
            return self._finish_card(base, mode, width, height, colors)
        except Exception as exc:  # noqa: BLE001
            log.warning("背景生成失败，退回纯色：%s", exc)
            return None

    def _finish_card(
        self, base: Image.Image, mode: str, width: int, height: int, colors: dict
    ) -> Image.Image:
        """底图 → 盖底色 → 切圆角 → 描边，得到最终卡片底图。"""
        opacity = self._scrim_opacity(mode, colors)
        tint = Image.new("RGB", (width, height), colors["tint"])
        blended = Image.blend(base.convert("RGB"), tint, opacity)

        # 切圆角
        mask = Image.new("L", (width, height), 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            [0, 0, width - 1, height - 1], radius=self.RADIUS, fill=255
        )
        card = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        card.paste(blended, (0, 0), mask)

        # 描一圈边，让卡片轮廓立住。
        # 这里不能用半透明色：layered window 不做 alpha 合成，半透明像素会和
        # 键控透明色混成一圈粉边。
        ImageDraw.Draw(card).rounded_rectangle(
            [0, 0, width - 1, height - 1],
            radius=self.RADIUS,
            outline=self._rgba(colors["border"], 255),
            width=1,
        )
        return card

    def _base_from_snapshot(
        self, mode: str, x: int, y: int, width: int, height: int
    ) -> Image.Image | None:
        """拖动预览用：从开局那张整屏快照里裁出窗口背后那块。

        先降采样再模糊，模糊半径跟着按比例缩小，视觉上和全分辨率差不多，
        但每帧开销只剩几分之一 —— 最贵的抓屏那一步已经被省掉了。
        """
        snapshot, meta = self._drag_snapshot, self._drag_meta
        if snapshot is None or meta is None:
            return None

        left, top, scale_x, scale_y = meta
        shot_w, shot_h = snapshot.size
        box = (
            min(shot_w, max(0, round((x - left) * scale_x))),
            min(shot_h, max(0, round((y - top) * scale_y))),
            min(shot_w, max(0, round((x - left + width) * scale_x))),
            min(shot_h, max(0, round((y - top + height) * scale_y))),
        )
        if box[2] <= box[0] or box[3] <= box[1]:
            return None
        region = snapshot.crop(box)

        step = max(1, self.LIVE_PREVIEW_DOWNSCALE)
        small = (max(1, width // step), max(1, height // step))
        if region.size != small:
            region = region.resize(small, Image.BILINEAR)

        if mode == "blur":
            base = region.filter(ImageFilter.GaussianBlur(11 / step))
        elif mode == "gradient":
            base = backgrounds.gradient(
                small, str(self.cfg.get("backdrop_gradient", ""))
            )
            base = Image.blend(
                base, region.filter(ImageFilter.GaussianBlur(18 / step)), 0.3
            )
        else:
            # image 模式预览：先用背后画面顶着，松手后再换回真正的图片
            base = region.filter(ImageFilter.GaussianBlur(18 / step))

        if base.size != (width, height):
            base = base.resize((width, height), Image.BILINEAR)
        return base

    def _scrim_opacity(self, mode: str, colors: dict) -> float:
        """底图上盖的底色浓度：保证文字在花哨背景上也能看清。"""
        if mode == "blur":
            return float(colors.get("tint_opacity", 0.84))
        return self._dim_opacity(colors)

    def _dim_opacity(self, colors: dict) -> float:
        try:
            value = float(self.cfg.get("backdrop_dim", 0.55))
        except (TypeError, ValueError):
            value = 0.55
        value = max(0.0, min(0.95, value))
        if colors is GLASS_LIGHT:
            # 浅色主题是深色字，底色得更实一点才压得住图片
            value = max(value, 0.66)
        return value

    def _grab_behind(
        self, x: int, y: int, width: int, height: int, win: tk.Toplevel
    ) -> Image.Image | None:
        """抓窗口背后那块屏幕。优先「截屏排除」，不支持的旧系统才隐藏窗口。"""
        excluded = False
        withdrawn = False
        try:
            if win.winfo_viewable():
                excluded = _hide_from_capture(win)
                if not excluded:
                    win.withdraw()
                    win.update()
                    withdrawn = True
            return self._grab_region(x, y, width, height)
        finally:
            if excluded:
                _show_in_capture(win)
            elif withdrawn:
                win.deiconify()

    def _draw_backdrop(
        self, canvas: tk.Canvas, width: int, height: int,
        colors: dict, image: ImageTk.PhotoImage | None,
    ) -> None:
        if image is not None:
            canvas.create_image(0, 0, anchor="nw", image=image, tags="bg")
        else:
            _rounded_rect(
                canvas, 0, 0, width, height, self.RADIUS,
                fill=colors["tint"], outline=colors["border"], width=1, tags="bg",
            )
        canvas.tag_lower("bg")

    @staticmethod
    def _grab_region(x: int, y: int, width: int, height: int) -> Image.Image | None:
        """按虚拟屏幕坐标抓一块图，兼容副屏在主屏左边（负坐标）和缩放比例。

        整屏拍完再裁，是因为 tkinter 的坐标、Windows 的虚拟屏幕原点、
        GDI 截图的像素尺寸在缩放环境下不一定一一对应 —— 用实际截图的
        尺寸反推比例，比猜 DPI 可靠。
        """
        left, top, virtual_w, virtual_h = desktop.virtual_screen()
        if virtual_w <= 0 or virtual_h <= 0:
            return None

        shot = ImageGrab.grab(all_screens=True)
        scale_x = shot.width / virtual_w
        scale_y = shot.height / virtual_h
        box = (
            round((x - left) * scale_x),
            round((y - top) * scale_y),
            round((x - left + width) * scale_x),
            round((y - top + height) * scale_y),
        )
        region = shot.crop(box)
        if region.size != (width, height):
            region = region.resize((width, height), Image.LANCZOS)
        return region

    @staticmethod
    def _rgba(hex_color: str, alpha: int) -> tuple[int, int, int, int]:
        text = hex_color.lstrip("#")
        if len(text) != 6:
            text = "3C4048"
        return (int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16), alpha)

    # ---- 内容排版 ----

    def _font(self, size: int, bold: bool = False) -> tkfont.Font:
        key = (size, bold)
        if key not in self._font_cache:
            self._font_cache[key] = tkfont.Font(
                family=FONT_CN, size=size, weight="bold" if bold else "normal"
            )
        return self._font_cache[key]

    def _layout(
        self, source, result, is_error, colors, font_size,
        max_width=None, force_width=None,
    ):
        """算出卡片内容宽度与整体高度，并返回「真正往画布上画」的闭包。

        分两步是因为：宽度决定卡片尺寸，尺寸决定卡片位置，位置决定该抓哪块屏。
        force_width 用于用户手动拖过宽度之后：这时宽度要严格跟着拖，
        不能再被「内容本来就这么宽」压回去。
        """
        max_width = int(max_width or self.cfg.get("bubble_width", 520))
        max_width = max(180, max_width)
        f_source = self._font(max(8, font_size - 2))
        f_phonetic = self._font(max(9, font_size - 1))
        f_body = self._font(font_size)
        f_example = self._font(max(9, font_size - 1))
        f_label = self._font(max(8, font_size - 3))
        f_note = self._font(max(8, font_size - 2))

        # 把内容组织成块，既方便测量也方便绘制
        blocks: list[tuple] = []
        # 原文和译文一模一样时就别重复占地方了（比如选中的本来就是中文）
        # 只在「无结构」结果上比：结构化结果的 raw 存的是模型原始输出，不是译文
        head = source.strip()
        duplicated = (not result.structured) and head == (result.raw or "").strip()
        if head and not duplicated:
            blocks.append(("text", source, f_source, colors["source"]))
            blocks.append(("line", "solid"))
            blocks.append(("space", self.GAP_AFTER_LINE))

        if is_error:
            blocks.append(("text", result.raw, f_body, colors["error"]))
        elif result.structured:
            if result.phonetic:
                blocks.append(("text", result.phonetic, f_phonetic, colors["phonetic"]))
                blocks.append(("space", self.GAP_AFTER_LINE - 3))
            for line in result.definitions:
                blocks.append(("text", line, f_body, colors["body"]))
                blocks.append(("space", self.GAP_LINE + 2))
            if result.examples:
                blocks.append(("space", self.GAP_AFTER_LINE - 5))
                blocks.append(("line", "dashed"))
                blocks.append(("space", self.GAP_AFTER_LINE - 4))
                blocks.append(("text", "例句", f_label, colors["label"]))
                blocks.append(("space", self.GAP_LINE + 4))
                blocks.append(("quote", result.examples, f_example, colors["example"]))
            if result.note:
                blocks.append(("space", self.GAP_AFTER_LINE))
                blocks.append(("text", result.note, f_note, colors["note"]))
        else:
            blocks.append(("text", result.raw, f_body, colors["body"]))

        # ---- 量宽度：所有可换行文本里最宽的一行 ----
        natural = 0
        for block in blocks:
            if block[0] == "text":
                natural = max(natural, self._measure(block[2], block[1]))
            elif block[0] == "quote":
                for line in block[1]:
                    natural = max(natural, self._measure(block[2], line) + self.QUOTE_INDENT)

        if force_width is not None:
            limit = self.MAX_CARD_W - self.PAD_X * 2
            content_w = max(180, min(limit, int(force_width)))
        else:
            content_w = max(180, min(max_width, natural))

        def painter() -> int:
            canvas = self._canvas
            assert canvas is not None
            y = self.PAD_Y
            quote_top: int | None = None

            for block in blocks:
                kind = block[0]
                if kind == "space":
                    y += block[1]
                elif kind == "line":
                    dash = None if block[1] == "solid" else (3, 3)
                    canvas.create_line(
                        self.PAD_X, y, self.PAD_X + content_w, y,
                        fill=colors["divider"] if dash is None else colors["dashed"],
                        dash=dash, tags="content",
                    )
                elif kind == "text":
                    _, text, font, fill = block
                    item = canvas.create_text(
                        self.PAD_X, y, text=text, width=content_w,
                        anchor="nw", font=font, fill=fill, tags="content",
                    )
                    y = canvas.bbox(item)[3]
                elif kind == "quote":
                    _, lines, font, fill = block
                    if quote_top is None:
                        quote_top = y
                    for line in lines:
                        item = canvas.create_text(
                            self.PAD_X + self.QUOTE_INDENT, y, text=line,
                            width=content_w - self.QUOTE_INDENT,
                            anchor="nw", font=font, fill=fill, tags="content",
                        )
                        y = canvas.bbox(item)[3] + self.GAP_LINE
                    # 例句左侧的引用竖线
                    canvas.create_line(
                        self.PAD_X + 3, quote_top, self.PAD_X + 3, y - self.GAP_LINE,
                        fill=colors["quote"], width=2, tags="content",
                    )

            return y

        # ---- 量高度：先在一张离屏画布上试排一次 ----
        probe = tk.Canvas(self.root, width=content_w, height=8,
                          highlightthickness=0, bd=0)
        keep, self._canvas = self._canvas, probe
        try:
            bottom = painter()
        finally:
            self._canvas = keep
            probe.destroy()

        return content_w, int(bottom + self.PAD_Y), painter

    @staticmethod
    def _measure(font: tkfont.Font, text: str) -> int:
        lines = text.splitlines() or [""]
        return max((font.measure(line) for line in lines), default=0)

    # ---- 定位 ----

    def _place(self, width: int, height: int) -> tuple[int, int]:
        """定位到鼠标右下角；贴边就翻到另一侧，且只在鼠标那块屏里活动。"""
        mouse_x, mouse_y = desktop.cursor_pos()
        left, top, right, bottom = desktop.monitor_work_area(mouse_x, mouse_y)

        x = mouse_x + 18
        y = mouse_y + 22

        if x + width > right - 8:
            x = mouse_x - width - 18
        if y + height > bottom - 8:
            y = mouse_y - height - 22

        x = max(left + 8, min(x, right - width - 8))
        y = max(top + 8, min(y, bottom - height - 8))
        return x, y

    # ---- 滚动 ----

    def _thumb_geometry(
        self, width: int, height: int
    ) -> tuple[int, int, int, int] | None:
        """返回 (轨道顶, 轨道高, 滑块顶, 滑块高)；不需要滚动条时为 None。"""
        if not self._scrollable or self._full_h <= self._view_h:
            return None
        track_top = self.PAD_Y
        track_h = max(24, height - self.PAD_Y * 2 - 6)
        thumb_h = min(
            track_h, max(30, round(track_h * self._view_h / max(1, self._full_h)))
        )
        span = max(1, track_h - thumb_h)
        ratio = self._scroll / max(1, self._full_h - self._view_h)
        return track_top, track_h, track_top + round(span * ratio), thumb_h

    def _draw_scrollbar(self, width: int, height: int, colors: dict) -> None:
        """画右侧滚动条和右下角缩放手柄。返回轨道顶部，方便命中测试复用。"""
        canvas = self._canvas
        if canvas is None:
            return
        canvas.delete("scroll")

        geo = self._thumb_geometry(width, height)
        if geo is not None:
            track_top, track_h, thumb_top, thumb_h = geo
            x1 = width - self.SCROLLBAR_W - 8
            x2 = x1 + self.SCROLLBAR_W
            canvas.create_line(
                (x1 + x2) / 2, track_top, (x1 + x2) / 2, track_top + track_h,
                fill=colors["dashed"], width=self.SCROLLBAR_W,
                capstyle="round", tags="scroll",
            )
            _rounded_rect(
                canvas, x1, thumb_top, x2, thumb_top + thumb_h, 3,
                fill=colors["label"], outline="", tags="scroll",
            )
            canvas.tag_raise("scroll")

        # 右下角三道斜线：提示这里可以拖大小
        gx, gy = width - 5, height - 5
        for offset in (0, 5, 10):
            canvas.create_line(
                gx - offset, gy, gx, gy - offset,
                fill=colors["label"], width=2, tags="scroll",
            )
        canvas.tag_raise("scroll")

    def _scroll_to(self, value: float) -> None:
        if not self._scrollable or self._canvas is None or self._size is None:
            return
        max_scroll = max(0, self._full_h - self._view_h)
        target = max(0, min(int(round(value)), max_scroll))
        if target == self._scroll:
            return
        delta = self._scroll - target
        self._scroll = target
        self._canvas.move("content", 0, delta)
        self._draw_scrollbar(self._size[0], self._size[1], self._colors or GLASS_DARK)

    def _scroll_from_track(self, y: int) -> int:
        if self._size is None:
            return self._scroll
        geo = self._thumb_geometry(*self._size)
        if geo is None:
            return self._scroll
        track_top, track_h, _thumb_top, thumb_h = geo
        span = max(1, track_h - thumb_h)
        ratio = (y - track_top - thumb_h / 2) / span
        ratio = max(0.0, min(1.0, ratio))
        return round(ratio * max(0, self._full_h - self._view_h))

    def _hit_scrollbar(self, x: int, y: int) -> bool:
        if self._size is None:
            return False
        width, height = self._size
        geo = self._thumb_geometry(width, height)
        if geo is None:
            return False
        track_top, track_h, _thumb_top, _thumb_h = geo
        return (
            width - self.SCROLLBAR_W - 12 <= x <= width - 6
            and track_top <= y <= track_top + track_h
        )

    def _hit_grip(self, x: int, y: int) -> bool:
        if self._size is None:
            return False
        width, height = self._size
        return x >= width - self.GRIP and y >= height - self.GRIP

    def _on_wheel(self, event, direction: int = 0) -> str:
        if not self._scrollable:
            return ""
        self._cancel_timer()
        if direction:
            notches = float(direction)
        else:
            delta = getattr(event, "delta", 0) or 0
            notches = delta / 120.0 if delta else 0.0
        if notches:
            self._scroll_to(self._scroll - notches * self.SCROLL_STEP)
        return "break"

    def _on_hover(self, event) -> None:
        win, size = self._win, self._size
        if win is None or size is None or not win.winfo_exists():
            return
        if self._dragging or self._resizing or self._scroll_drag:
            return
        width, height = size
        if self._hit_grip(event.x, event.y):
            cursor = "size_nw_se"
        elif event.x >= width - self.EDGE:
            cursor = "sb_h_double_arrow"
        elif event.y >= height - self.EDGE:
            cursor = "sb_v_double_arrow"
        elif self._hit_scrollbar(event.x, event.y):
            cursor = "hand2"
        else:
            cursor = "fleur"
        if cursor != self._cursor:
            self._cursor = cursor
            win.configure(cursor=cursor)

    # ---- 拖动 / 缩放 ----

    def _on_press(self, event) -> None:
        self._cancel_timer()
        if self._size is not None:
            width, height = self._size
            if self._hit_grip(event.x, event.y):
                self._begin_resize(event, True, True)
                return
            if self._hit_scrollbar(event.x, event.y):
                self._scroll_drag = True
                self._scroll_to(self._scroll_from_track(event.y))
                return
            resize_w = event.x >= width - self.EDGE
            resize_h = event.y >= height - self.EDGE
            if resize_w or resize_h:
                self._begin_resize(event, resize_w, resize_h)
                return

        self._dragging = True
        self._dragged = False
        self._drag_start = (event.x_root, event.y_root)
        if self._pos is None:
            return
        self._drag_offset = (
            event.x_root - self._pos[0],
            event.y_root - self._pos[1],
        )

    def _begin_resize(self, event, resize_w: bool, resize_h: bool) -> None:
        if self._size is None:
            return
        self._resizing = True
        width, height = self._size
        self._resize_origin = (
            event.x_root, event.y_root, width, height, resize_w, resize_h,
        )

    def _resize_motion(self, event) -> None:
        if self._resize_origin is None:
            return
        start_x, start_y, width0, height0, resize_w, resize_h = self._resize_origin
        new_w = width0 + (event.x_root - start_x) if resize_w else width0
        new_h = height0 + (event.y_root - start_y) if resize_h else height0
        new_w = max(self.MIN_CARD_W, min(self.MAX_CARD_W, new_w))
        new_h = max(self.MIN_VIEW_H, min(self._max_view_height(), new_h))
        self._pending_size = (int(new_w), int(new_h))
        if self._resize_job is None:
            # 拖动期间 30ms 合并一次重排，别每个像素都排版一遍
            self._resize_job = self.root.after(30, self._apply_resize)

    def _apply_resize(self, final: bool = False) -> None:
        self._resize_job = None
        if self._colors is None:
            return
        changed = False
        if self._pending_size is not None:
            width, height = self._pending_size
            self._pending_size = None
            self._width_override = max(180, width - self.PAD_X * 2)
            self._height_override = height
            changed = True
        if self._width_override is None:
            return
        if final:
            # 松手后重拍全质量背景，把预览帧换掉
            self._render(self._colors, reposition=False)
        elif changed:
            # 拖动过程中不抓屏：用开局那张快照裁出预览背景（不再是纯色卡片）
            self._ensure_drag_snapshot()
            self._render(self._colors, reposition=False, preview=True)

    def _on_motion(self, event) -> None:
        win = self._win
        if win is None or not win.winfo_exists():
            return
        if self._scroll_drag:
            offset = self._pos[1] if self._pos is not None else 0
            self._scroll_to(self._scroll_from_track(event.y_root - offset))
            return
        if self._resizing:
            self._resize_motion(event)
            return
        if self._drag_start is None or self._drag_offset is None:
            return
        if self._size is None:
            return

        dx = event.x_root - self._drag_start[0]
        dy = event.y_root - self._drag_start[1]
        if not self._dragged and abs(dx) + abs(dy) < 4:
            return  # 只是手抖，别把点击当成拖动

        self._dragged = True
        width, height = self._size
        x = event.x_root - self._drag_offset[0]
        y = event.y_root - self._drag_offset[1]
        x, y = desktop.clamp_to_work_area(x, y, width, height)
        self._pos = (x, y)
        place_window(win, x, y, width, height)

    def _on_release(self, _event) -> None:
        if self._scroll_drag:
            self._scroll_drag = False
            if not self._cursor_inside():
                self._arm_timer()
            return

        if self._resizing:
            self._resizing = False
            self._resize_origin = None
            if self._resize_job is not None:
                try:
                    self.root.after_cancel(self._resize_job)
                except (ValueError, tk.TclError):
                    pass
                self._resize_job = None
            self._apply_resize(final=True)
            self._clear_drag_snapshot()
            if not self._cursor_inside():
                self._arm_timer()
            return

        self._dragging = False
        self._drag_start = None
        self._drag_offset = None
        self._clear_drag_snapshot()

        if not self._dragged:
            self.hide()  # 没挪动就是点击，沿用「点一下关掉」
            return

        self._dragged = False
        self._repaint_backdrop()
        if not self._cursor_inside():
            self._arm_timer()

    def _cursor_inside(self) -> bool:
        if self._pos is None or self._size is None:
            return False
        x, y = self.root.winfo_pointerxy()
        return _in_rect(x, y, (*self._pos, *self._size))

    def _repaint_backdrop(self) -> None:
        """拖动松手后按新位置重新抓屏，否则毛玻璃还是旧位置的画面。"""
        win, canvas = self._win, self._canvas
        if win is None or canvas is None or not win.winfo_exists():
            return
        if not win.winfo_viewable():
            return
        if self._pos is None or self._size is None:
            return
        width, height = self._size
        if width <= 1 or height <= 1:
            return

        x, y = self._pos
        colors = self._pick_palette()
        self._colors = colors
        self._photo = None
        self._photo_key = None
        # 先拍新图（旧背景还在屏幕上，抓屏时窗口会被排除），
        # 再把背景一次性换掉 —— 不 withdraw，也不留空白帧。
        image = self._backdrop_image(x, y, width, height, colors, win)
        canvas.delete("bg")
        self._draw_backdrop(canvas, width, height, colors, image)

    # ---- 缩放时的背景预览 ----

    def _live_preview_enabled(self) -> bool:
        if not bool(self.cfg.get("backdrop_live_resize", True)):
            return False
        # 纯色模式本来就没什么可重画的
        return self._backdrop_mode() != "solid"

    def _ensure_drag_snapshot(self) -> bool:
        """确保本次缩放有一张可以反复裁剪的整屏快照。

        一次缩放只尝试拍一次：拿不到就安静退化，预览退回纯色卡片。
        """
        if self._drag_snapshot is not None:
            return True
        if not self._live_preview_enabled() or self._live_attempted:
            return False
        self._live_attempted = True
        self._capture_drag_snapshot()
        return self._drag_snapshot is not None

    def _capture_drag_snapshot(self) -> None:
        """抓一张整屏快照，缩放预览时每帧只从里面裁。

        窗口先被排除出截屏再拍，否则拍到的就是气泡自己。抓屏是整个流程里
        最贵的一步（十几到几十毫秒），一次缩放只付一次。
        """
        win = self._win
        if win is None or not win.winfo_exists() or not win.winfo_viewable():
            return
        if not _hide_from_capture(win):
            log.debug("拖动快照不可用（截屏排除失败），退回松手后重拍")
            return
        try:
            snapshot = ImageGrab.grab(all_screens=True).convert("RGB")
            left, top, virtual_w, virtual_h = desktop.virtual_screen()
        except Exception as exc:  # noqa: BLE001
            log.debug("拖动快照失败：%s", exc)
            return
        finally:
            _show_in_capture(win)
        if virtual_w <= 0 or virtual_h <= 0:
            return
        self._drag_snapshot = snapshot
        self._drag_meta = (
            left,
            top,
            snapshot.width / virtual_w,
            snapshot.height / virtual_h,
        )

    def _clear_drag_snapshot(self) -> None:
        self._drag_snapshot = None
        self._drag_meta = None
        self._live_attempted = False

    def hwnd(self) -> int:
        """窗口句柄；没创建或已销毁时返回 0。"""
        win = self._win
        if win is None or not win.winfo_exists():
            return 0
        try:
            return _root_hwnd(win)
        except tk.TclError:
            return 0

    # ---- 自动收起 ----

    def _arm_timer(self) -> None:
        if self._dragging or self._resizing or self._scroll_drag:
            return  # 拖动 / 缩放 / 拖滚动条的过程中别被自动收起
        self._cancel_timer()
        timeout = float(self.cfg.get("bubble_timeout", 7.0))
        if timeout <= 0:
            return
        self._job = self.root.after(int(timeout * 1000), self.hide)

    def _cancel_timer(self) -> None:
        if self._job is not None:
            try:
                self.root.after_cancel(self._job)
            except (ValueError, tk.TclError):
                pass
            self._job = None


# ------------------------------------------------------------ 划词按钮


class SelectionPrompt:
    """划词后贴在鼠标旁的「翻译」小按钮。

    和气泡一样打 WS_EX_NOACTIVATE：点它的时候焦点仍在原程序里，
    选区没有丢，后台照常发 Ctrl+C 就能把选中的字取回来。
    """

    WIDTH = 60
    HEIGHT = 28
    RADIUS = 14
    GAP_X = 10
    GAP_Y = 12
    TIMEOUT = 2.5
    BG = "#2563EB"
    BG_HOVER = "#1D4ED8"
    BORDER = "#1E3A8A"

    def __init__(self, root: tk.Tk, on_click) -> None:
        self.root = root
        self.on_click = on_click
        self._win: tk.Toplevel | None = None
        self._canvas: tk.Canvas | None = None
        self._font: tkfont.Font | None = None
        self._job: str | None = None
        self._rect: tuple[int, int, int, int] | None = None

    # ---- 窗口 ----

    def _ensure_window(self) -> tk.Toplevel:
        if self._win is not None and self._win.winfo_exists():
            return self._win

        win = tk.Toplevel(self.root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(bg=TRANSPARENT_KEY, cursor="hand2")
        try:
            win.attributes("-transparentcolor", TRANSPARENT_KEY)
        except tk.TclError:
            log.debug("当前环境不支持键控透明，翻译按钮会退化为直角")

        canvas = tk.Canvas(win, bg=TRANSPARENT_KEY, highlightthickness=0, bd=0)
        canvas.pack(fill="both", expand=True)
        _apply_no_activate(win)

        win.bind("<Enter>", lambda _e: self._hover(True))
        win.bind("<Leave>", lambda _e: self._hover(False))
        win.bind("<ButtonRelease-1>", self._on_click)

        self._win = win
        self._canvas = canvas
        return win

    def show(self, x: int, y: int) -> None:
        win = self._ensure_window()
        canvas = self._canvas
        assert canvas is not None

        self._draw(canvas, hover=False)
        px, py = self._place(x, y)

        self._rect = (px, py, self.WIDTH, self.HEIGHT)
        place_window(win, px, py, self.WIDTH, self.HEIGHT)
        win.deiconify()
        win.lift()
        self._arm_timer()

    def hide(self) -> None:
        self._cancel_timer()
        if self._win is not None and self._win.winfo_exists():
            self._win.withdraw()

    def hit(self, x: int, y: int) -> bool:
        """按最后一次出现的位置判断，按钮刚被收起也算。

        全局鼠标监视和 tkinter 的点击事件是两条线：点按钮时监视线程
        可能先把按钮收掉，导致这一下点击落空。用位置判断可以避开。
        """
        return self._rect is not None and _in_rect(x, y, self._rect)

    def hwnd(self) -> int:
        win = self._win
        if win is None or not win.winfo_exists():
            return 0
        try:
            return _root_hwnd(win)
        except tk.TclError:
            return 0

    # ---- 绘制与交互 ----

    def _draw(self, canvas: tk.Canvas, hover: bool) -> None:
        canvas.delete("all")
        _rounded_rect(
            canvas, 0, 0, self.WIDTH, self.HEIGHT, self.RADIUS,
            fill=self.BG_HOVER if hover else self.BG,
            outline=self.BORDER, width=1,
        )
        if self._font is None:
            self._font = tkfont.Font(family=FONT_CN, size=9, weight="bold")
        canvas.create_text(
            self.WIDTH // 2, self.HEIGHT // 2,
            text="翻译", font=self._font, fill="#FFFFFF",
        )

    def _hover(self, hover: bool) -> None:
        canvas = self._canvas
        win = self._win
        if canvas is None or win is None or not win.winfo_exists():
            return
        if not win.winfo_viewable():
            return
        self._draw(canvas, hover)
        if hover:
            self._cancel_timer()
        else:
            self._arm_timer()

    def _on_click(self, _event) -> None:
        self.hide()
        try:
            self.on_click()
        except Exception:  # noqa: BLE001
            log.exception("翻译按钮回调失败")

    def _place(self, x: int, y: int) -> tuple[int, int]:
        left, top, right, bottom = desktop.monitor_work_area(x, y)
        px = x + self.GAP_X
        py = y + self.GAP_Y
        if px + self.WIDTH > right - 4:
            px = x - self.WIDTH - self.GAP_X
        if py + self.HEIGHT > bottom - 4:
            py = y - self.HEIGHT - self.GAP_Y
        px = max(left + 4, min(px, right - self.WIDTH - 4))
        py = max(top + 4, min(py, bottom - self.HEIGHT - 4))
        return px, py

    # ---- 自动收起 ----

    def _arm_timer(self) -> None:
        self._cancel_timer()
        self._job = self.root.after(int(self.TIMEOUT * 1000), self.hide)

    def _cancel_timer(self) -> None:
        if self._job is not None:
            try:
                self.root.after_cancel(self._job)
            except (ValueError, tk.TclError):
                pass
            self._job = None


# ------------------------------------------------------------ 设置窗口


ENGINE_LABELS = [("baidu", "百度翻译"), ("ai", "AI 大模型")]
BACKDROP_LABELS = [
    ("blur", "毛玻璃"),
    ("gradient", "渐变玻璃"),
    ("image", "自定义图片"),
    ("solid", "纯色卡片"),
]


class SettingsWindow:
    """一个窗口搞定所有配置：引擎、密钥、热键、气泡外观、开机自启。"""

    def __init__(self, root: tk.Tk, cfg: dict, on_save) -> None:
        self.root = root
        self.cfg = cfg
        self.on_save = on_save
        self.win: tk.Toplevel | None = None

    def open(self) -> None:
        if self.win is not None and self.win.winfo_exists():
            self.win.deiconify()
            self.win.lift()
            self.win.focus_force()
            return

        win = tk.Toplevel(self.root)
        self.win = win
        win.title(f"{config_mod.APP_TITLE} · 设置")
        win.resizable(True, True)
        win.minsize(580, 400)

        style = ttk.Style(win)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        win.option_add("*Font", (FONT_CN, 9))

        self._build_scrollable(win)
        height = min(880, max(520, win.winfo_screenheight() - 140))
        self._center(win, 700, height)
        win.protocol("WM_DELETE_WINDOW", self._close)

    # ---- 构建界面 ----

    def _build_scrollable(self, win: tk.Toplevel) -> None:
        """内容装进可滚动画布：小屏幕上也能把所有设置项看全。"""
        canvas = tk.Canvas(
            win, highlightthickness=0, bd=0, bg=win.cget("background")
        )
        bar = ttk.Scrollbar(win, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        outer = ttk.Frame(canvas, padding=(18, 14, 18, 12))
        window_id = canvas.create_window((0, 0), window=outer, anchor="nw")

        def sync(_event=None) -> None:
            if not canvas.winfo_exists():
                return
            canvas.configure(scrollregion=canvas.bbox("all"))
            canvas.itemconfigure(window_id, width=max(canvas.winfo_width(), 1))

        outer.bind("<Configure>", sync)
        canvas.bind("<Configure>", sync)
        # 绑在顶层窗口上，子控件里的滚轮也能转到画布（bindtags 会一路冒上来）
        win.bind("<MouseWheel>", lambda event: canvas.yview_scroll(
            -1 if event.delta > 0 else 1, "units"
        ))

        self._settings_canvas = canvas
        self._build(outer)

    def _build(self, outer: ttk.Frame) -> None:
        self.engine_var = tk.StringVar(value=self.cfg.get("engine", "baidu"))
        self.hotkey_var = tk.StringVar(value=self.cfg.get("hotkey", "ctrl+alt+t"))
        self.backdrop_var = tk.StringVar(value=self.cfg.get("backdrop", "blur"))
        self.gradient_labels = backgrounds.preset_labels()
        self.gradient_var = tk.StringVar(
            value=backgrounds.preset_label(
                str(self.cfg.get("backdrop_gradient", backgrounds.DEFAULT_GRADIENT))
            )
        )
        self.image_var = tk.StringVar(value=str(self.cfg.get("backdrop_image", "")))
        self.image_blur_var = tk.IntVar(
            value=int(self.cfg.get("backdrop_image_blur", 0) or 0)
        )
        self.dim_var = tk.DoubleVar(
            value=float(self.cfg.get("backdrop_dim", 0.55) or 0.55)
        )
        self.baidu_appid = tk.StringVar(value=self.cfg["baidu"].get("appid", ""))
        self.baidu_secret = tk.StringVar(value=self.cfg["baidu"].get("secret", ""))
        self.ai_base = tk.StringVar(value=self.cfg["ai"].get("base_url", ""))
        self.ai_model = tk.StringVar(value=self.cfg["ai"].get("model", ""))
        self.ai_key = tk.StringVar(value=self.cfg["ai"].get("api_key", ""))
        self.timeout_var = tk.DoubleVar(value=float(self.cfg.get("bubble_timeout", 7.0)))
        self.font_var = tk.IntVar(value=int(self.cfg.get("font_size", 11)))
        self.selection_var = tk.BooleanVar(
            value=bool(self.cfg.get("selection_button", True))
        )
        self.strict_var = tk.BooleanVar(
            value=bool(self.cfg.get("selection_button_strict", True))
        )
        self.precise_var = tk.BooleanVar(
            value=bool(self.cfg.get("selection_button_precise", True))
        )
        self.autostart_var = tk.BooleanVar(value=bool(self.cfg.get("autostart", False)))

        row = 0

        row = self._section(outer, "翻译引擎", row)
        engine_box = ttk.Frame(outer)
        engine_box.grid(row=row, column=0, columnspan=2, sticky="w", pady=(0, 4))
        for value, label in ENGINE_LABELS:
            ttk.Radiobutton(
                engine_box, text=label, value=value, variable=self.engine_var
            ).pack(side="left", padx=(0, 18))
        row += 1

        row = self._section(outer, "触发快捷键", row)
        hk_box = ttk.Frame(outer)
        hk_box.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(0, 4))
        ttk.Entry(hk_box, textvariable=self.hotkey_var, width=24).pack(side="left")
        ttk.Button(hk_box, text="点击录制", command=self._record_hotkey).pack(
            side="left", padx=(8, 0)
        )
        ttk.Label(
            hk_box, text="点「录制」后直接按下组合键", foreground="#888888"
        ).pack(side="left", padx=(10, 0))
        row += 1

        row = self._section(outer, "百度翻译（开放平台 → 通用文本翻译）", row)
        row = self._field(outer, row, "APPID", self.baidu_appid)
        row = self._field(outer, row, "密钥", self.baidu_secret)

        row = self._section(outer, "AI 大模型（任何 OpenAI 兼容接口都行）", row)
        preset_box = ttk.Frame(outer)
        preset_box.grid(row=row, column=0, columnspan=2, sticky="w", pady=(0, 4))
        ttk.Label(preset_box, text="快速预设").pack(side="left", padx=(0, 10))
        preset = ttk.Combobox(
            preset_box, values=list(config_mod.AI_PRESETS.keys()),
            state="readonly", width=16,
        )
        preset.pack(side="left")
        preset.bind("<<ComboboxSelected>>", self._apply_preset)
        row += 1
        row = self._field(outer, row, "接口地址", self.ai_base)
        row = self._field(outer, row, "模型名", self.ai_model)
        row = self._field(outer, row, "API Key", self.ai_key)

        row = self._section(outer, "气泡外观", row)
        backdrop_box = ttk.Frame(outer)
        backdrop_box.grid(row=row, column=0, columnspan=2, sticky="w", pady=(0, 4))
        for value, label in BACKDROP_LABELS:
            ttk.Radiobutton(
                backdrop_box, text=label, value=value, variable=self.backdrop_var
            ).pack(side="left", padx=(0, 16))
        row += 1

        grad_box = ttk.Frame(outer)
        grad_box.grid(row=row, column=0, columnspan=2, sticky="w", pady=(0, 4))
        ttk.Label(grad_box, text="渐变配色").pack(side="left", padx=(22, 6))
        ttk.Combobox(
            grad_box, state="readonly", width=14,
            values=list(self.gradient_labels.keys()),
            textvariable=self.gradient_var,
        ).pack(side="left")
        ttk.Label(
            grad_box, text="「毛玻璃」模式不用这一项", foreground="#888888"
        ).pack(side="left", padx=(10, 0))
        row += 1

        image_box = ttk.Frame(outer)
        image_box.grid(row=row, column=0, columnspan=2, sticky="w", pady=(0, 4))
        ttk.Label(image_box, text="背景图片").pack(side="left", padx=(22, 6))
        ttk.Entry(image_box, textvariable=self.image_var, width=40).pack(side="left")
        ttk.Button(image_box, text="浏览…", command=self._pick_image).pack(
            side="left", padx=(6, 0)
        )
        ttk.Button(
            image_box, text="清除", width=6,
            command=lambda: self.image_var.set(""),
        ).pack(side="left", padx=(4, 0))
        row += 1

        tune_box = ttk.Frame(outer)
        tune_box.grid(row=row, column=0, columnspan=2, sticky="w", pady=(0, 4))
        ttk.Label(tune_box, text="图片模糊").pack(side="left", padx=(22, 6))
        ttk.Spinbox(
            tune_box, from_=0, to=40, width=4, textvariable=self.image_blur_var
        ).pack(side="left")
        ttk.Label(tune_box, text="像素").pack(side="left", padx=(2, 18))
        ttk.Label(tune_box, text="背景浓度").pack(side="left")
        ttk.Spinbox(
            tune_box, from_=0, to=0.95, increment=0.05, width=5,
            textvariable=self.dim_var,
        ).pack(side="left", padx=4)
        ttk.Label(tune_box, text="（0 全透 - 0.95 几乎不透明）").pack(side="left")
        row += 1

        bubble_box = ttk.Frame(outer)
        bubble_box.grid(row=row, column=0, columnspan=2, sticky="w", pady=(2, 4))
        ttk.Label(bubble_box, text="停留").pack(side="left")
        ttk.Spinbox(
            bubble_box, from_=0, to=60, increment=1, width=5, textvariable=self.timeout_var
        ).pack(side="left", padx=4)
        ttk.Label(bubble_box, text="秒（0 = 不自动关闭）").pack(side="left", padx=(0, 20))
        ttk.Label(bubble_box, text="字号").pack(side="left")
        ttk.Spinbox(
            bubble_box, from_=8, to=22, width=4, textvariable=self.font_var
        ).pack(side="left", padx=4)
        row += 1

        ttk.Checkbutton(
            outer, text="选中文字后，在鼠标旁显示「翻译」小按钮",
            variable=self.selection_var,
        ).grid(row=row, column=0, columnspan=2, sticky="w", pady=(0, 4))
        row += 1

        ttk.Checkbutton(
            outer, text="仅在确认是文字控件时显示（严格模式）",
            variable=self.strict_var,
        ).grid(row=row, column=0, columnspan=2, sticky="w", padx=(20, 0), pady=(0, 2))
        row += 1

        ttk.Label(
            outer,
            text="严格模式下原生文本控件直接弹；浏览器/编辑器无法确认时，"
                 "可勾选下面的精确检测临时复制一次来确认。",
            foreground="#888888", wraplength=560,
        ).grid(row=row, column=0, columnspan=2, sticky="w", padx=(20, 0), pady=(0, 4))
        row += 1

        ttk.Checkbutton(
            outer, text="精确检测（无法确认时临时复制一次）",
            variable=self.precise_var,
        ).grid(row=row, column=0, columnspan=2, sticky="w", padx=(40, 0), pady=(0, 2))
        row += 1

        ttk.Label(
            outer,
            text="会短暂占用剪贴板并随后还原；选中内容可能进入 Win+V 剪贴板历史。",
            foreground="#888888", wraplength=560,
        ).grid(row=row, column=0, columnspan=2, sticky="w", padx=(40, 0), pady=(0, 6))
        row += 1

        ttk.Checkbutton(
            outer, text="开机自动启动（随 Windows 一起静默运行）",
            variable=self.autostart_var,
        ).grid(row=row, column=0, columnspan=2, sticky="w", pady=(0, 8))
        row += 1

        status = ttk.Label(outer, text="", foreground="#888888", wraplength=550)
        status.grid(row=row, column=0, columnspan=2, sticky="w", pady=(4, 6))
        self.status_label = status
        row += 1

        buttons = ttk.Frame(outer)
        buttons.grid(row=row, column=0, columnspan=2, sticky="e")
        ttk.Button(buttons, text="测试连接", command=self._test).pack(
            side="left", padx=(0, 8)
        )
        ttk.Button(buttons, text="保存", command=self._save).pack(
            side="left", padx=(0, 8)
        )
        ttk.Button(buttons, text="关闭", command=self._close).pack(side="left")

    def _section(self, parent: ttk.Frame, title: str, row: int) -> int:
        ttk.Label(parent, text=title, font=(FONT_CN, 10, "bold")).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(12, 6)
        )
        return row + 1

    def _field(self, parent: ttk.Frame, row: int, label: str, var: tk.Variable) -> int:
        ttk.Label(parent, text=label, width=10, anchor="e").grid(
            row=row, column=0, sticky="e", padx=(0, 10), pady=3
        )
        ttk.Entry(parent, textvariable=var, width=52).grid(
            row=row, column=1, sticky="w", pady=3
        )
        return row + 1

    @staticmethod
    def _center(win: tk.Toplevel, width: int, height: int) -> None:
        win.update_idletasks()
        screen_w = win.winfo_screenwidth()
        screen_h = win.winfo_screenheight()
        x = (screen_w - width) // 2
        y = max(20, (screen_h - height) // 2 - 30)
        win.geometry(f"{width}x{height}+{x}+{y}")

    # ---- 交互 ----

    def _apply_preset(self, event) -> None:
        preset = config_mod.AI_PRESETS.get(event.widget.get())
        if preset:
            self.ai_base.set(preset["base_url"])
            self.ai_model.set(preset["model"])

    def _pick_image(self) -> None:
        path = filedialog.askopenfilename(
            parent=self.win,
            title="选择气泡背景图片",
            filetypes=[
                ("图片", "*.png *.jpg *.jpeg *.bmp *.webp *.gif *.tif *.tiff"),
                ("所有文件", "*.*"),
            ],
        )
        if path:
            self.image_var.set(path)

    def _record_hotkey(self) -> None:
        """临时挂上键盘钩子，把用户按下的组合键直接填进输入框。"""
        self.hotkey_var.set("请按下组合键…")

        def worker() -> None:
            try:
                captured = keyboard.read_hotkey(suppress=False)
            except Exception as exc:  # noqa: BLE001
                self._set_status(f"录制失败：{exc}", error=True)
                return
            self.root.after(0, lambda: self.hotkey_var.set(captured))

        threading.Thread(target=worker, daemon=True).start()

    def _collect(self) -> dict:
        cfg = config_mod.load()
        cfg["engine"] = self.engine_var.get()
        cfg["hotkey"] = self.hotkey_var.get().strip() or "ctrl+alt+t"
        cfg["backdrop"] = self.backdrop_var.get()
        cfg["backdrop_gradient"] = self.gradient_labels.get(
            self.gradient_var.get(), backgrounds.DEFAULT_GRADIENT
        )
        cfg["backdrop_image"] = self.image_var.get().strip()
        cfg["backdrop_image_blur"] = max(0, min(40, int(self.image_blur_var.get())))
        cfg["backdrop_dim"] = max(0.0, min(0.95, float(self.dim_var.get())))
        cfg["bubble_timeout"] = float(self.timeout_var.get())
        cfg["font_size"] = int(self.font_var.get())
        cfg["selection_button"] = bool(self.selection_var.get())
        cfg["selection_button_strict"] = bool(self.strict_var.get())
        cfg["selection_button_precise"] = bool(self.precise_var.get())
        cfg["autostart"] = bool(self.autostart_var.get())
        cfg["baidu"]["appid"] = self.baidu_appid.get().strip()
        cfg["baidu"]["secret"] = self.baidu_secret.get().strip()
        cfg["ai"]["base_url"] = self.ai_base.get().strip()
        cfg["ai"]["model"] = self.ai_model.get().strip()
        cfg["ai"]["api_key"] = self.ai_key.get().strip()
        return cfg

    def _save(self) -> None:
        try:
            cfg = self._collect()
        except tk.TclError:
            self._set_status(
                "请检查填写内容：停留时间、字号、模糊和背景浓度必须是数字", error=True
            )
            return
        try:
            self.on_save(cfg)
        except Exception as exc:  # noqa: BLE001
            self._set_status(f"保存失败：{exc}", error=True)
            return
        # 已经存下了。这个组合要是有坑，顺嘴提一句 —— 但绝不拦着不让用
        advice = system.hotkey_advice(cfg["hotkey"])
        self._set_status(
            f"已保存，立即生效　{advice}" if advice else "已保存，立即生效",
            warn=bool(advice),
        )

    def _test(self) -> None:
        try:
            cfg = self._collect()
        except tk.TclError:
            self._set_status("请检查填写内容", error=True)
            return

        engine = build_engine(cfg)
        ok, reason = engine.ready()
        if not ok:
            self._set_status(reason, error=True)
            return

        engine_name = "百度翻译" if cfg["engine"] == "baidu" else "AI 大模型"
        self._set_status(f"正在用{engine_name}测试…")

        def worker() -> None:
            try:
                result = engine.translate("hello world")
                text = result.raw.replace("\n", " / ")
                self.root.after(
                    0, lambda: self._set_status(f"测试成功：hello world → {text[:70]}")
                )
            except Exception as exc:  # noqa: BLE001
                self.root.after(
                    0, lambda: self._set_status(f"测试失败：{exc}", error=True)
                )

        threading.Thread(target=worker, daemon=True).start()

    def _set_status(self, text: str, error: bool = False, warn: bool = False) -> None:
        if self.win is None or not self.win.winfo_exists():
            return
        if error:
            color = "#D93025"
        elif warn:
            color = "#B26A00"      # 提醒用琥珀色，别跟「出错」的红混在一起
        else:
            color = "#1A7F37"
        self.status_label.configure(text=text, foreground=color)

    def hwnd(self) -> int:
        """窗口句柄；没打开或已关闭时返回 0。"""
        win = self.win
        if win is None or not win.winfo_exists():
            return 0
        try:
            return _root_hwnd(win)
        except tk.TclError:
            return 0

    def _close(self) -> None:
        if self.win is not None and self.win.winfo_exists():
            self.win.destroy()
        self.win = None
