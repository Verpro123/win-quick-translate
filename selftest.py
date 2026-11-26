# -*- coding: utf-8 -*-
"""自检脚本：逐项验证运行环境与各模块是否正常。

用法（在项目目录下）：
    python selftest.py

任何一项显示 FAIL，都会给出具体原因。
"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

PASS = "  OK  "
FAIL = " FAIL "


def check(label, func):
    try:
        detail = func()
        print(f"[{PASS}] {label}" + (f" —— {detail}" if detail else ""))
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"[{FAIL}] {label} —— {type(exc).__name__}: {exc}")
        traceback.print_exc()
        return False


def main() -> int:
    results = []

    results.append(check("导入标准库与第三方库", _imports))
    results.append(check("读取/写入配置", _config))
    results.append(check("剪贴板读写的往返", _clipboard))
    results.append(check("全局热键注册与注销", _hotkey))
    results.append(check("屏幕几何与多屏坐标", _desktop))
    results.append(check("生成渐变 / 图片背景", _backgrounds))
    results.append(check("创建气泡与划词按钮", _bubble))
    results.append(check("绘制托盘图标", _icon))
    results.append(check("开机自启动读写权限", _autostart))
    results.append(check("单实例互斥锁", _mutex))

    ok = sum(1 for r in results if r)
    total = len(results)
    print(f"\n结果：{ok}/{total} 项通过")
    return 0 if ok == total else 1


def _imports():
    import config, engines, capture, system, ui, tray, desktop  # noqa: F401
    import tkinter

    return f"Python {sys.version.split()[0]} / Tk {tkinter.TkVersion}"


def _config():
    import config

    cfg = config.load()
    return f"路径 {config.config_path()}，默认热键 {cfg['hotkey']}"


def _clipboard():
    import capture

    original = capture.get_clipboard_text()
    probe = "QuickTranslate 剪贴板自检 probe"
    if not capture.set_clipboard_text(probe):
        raise RuntimeError("写入剪贴板失败")
    read_back = capture.get_clipboard_text()
    if read_back != probe:
        raise RuntimeError(f"写进去的和读出来的不一致：{read_back!r}")
    if original is not None:
        capture.set_clipboard_text(original)
    return "读写一致，原内容已还原"


def _hotkey():
    import system

    manager = system.HotkeyManager(lambda: None)
    combo = manager.register("ctrl+alt+f9")
    manager.unregister()
    return f"注册并注销 {combo}"


def _desktop():
    import desktop

    x, y = desktop.cursor_pos()
    left, top, right, bottom = desktop.monitor_work_area(x, y)
    if right <= left or bottom <= top:
        raise RuntimeError(f"工作区异常：{left},{top},{right},{bottom}")
    vleft, vtop, vw, vh = desktop.virtual_screen()

    cases = [
        # 资源管理器文件区（Win10 两种层级）、导航树、任务栏：不是选字
        (["DirectUIHWND", "DUIViewWndClassName", "ShellTabWindowClass", "CabinetWClass"], False),
        (["DirectUIHWND", "SHELLDLL_DefView", "CtrlNotifySink", "DirectUIHWND"], False),
        (["SysTreeView32", "NamespaceTreeControl", "CtrlNotifySink", "#32770"], False),
        (["MSTaskListWClass", "Shell_TrayWnd"], False),
        # 浏览器 / 普通窗口的文本区：可以划词
        (["Chrome_RenderWidgetHostHWND", "Chrome_WidgetWin_1"], True),
        (["TkChild", "TkTopLevel"], True),
    ]
    for chain, expected in cases:
        if desktop.chain_is_text_surface(chain) != expected:
            raise RuntimeError(f"控件过滤判断错误：{chain} 应返回 {expected}")

    for leaf, expected in (
        ("Edit", True),
        ("_WwG", True),
        ("Scintilla", True),
        ("Chrome_RenderWidgetHostHWND", False),
        ("TkChild", False),
    ):
        if desktop.class_is_confirmed_text(leaf) != expected:
            raise RuntimeError(f"严格文本判断错误：{leaf} 应返回 {expected}")

    for leaf, expected in (
        ("Chrome_RenderWidgetHostHWND", True),
        ("MozillaContentWindowClass", True),
        ("Edit", False),
    ):
        if desktop.is_browser_surface(leaf) != expected:
            raise RuntimeError(f"浏览器控件判断错误：{leaf} 应返回 {expected}")

    return (
        f"鼠标 ({x},{y}) 在当前屏 {right - left}×{bottom - top} 内；"
        f"虚拟屏幕 {vw}×{vh} @ ({vleft},{vtop})；外壳控件过滤正常"
    )


def _backgrounds():
    import backgrounds

    image = backgrounds.gradient((320, 480), "aurora")
    if image.size != (320, 480) or image.mode != "RGB":
        raise RuntimeError("渐变图尺寸或模式不对")
    if backgrounds.load_image("", (16, 16)) is not None:
        raise RuntimeError("空图片路径应该返回 None")
    return f"{len(backgrounds.GRADIENT_PRESETS)} 套渐变，默认 {backgrounds.DEFAULT_GRADIENT}"


def _bubble():
    import types
    import tkinter as tk

    import config
    import desktop
    import ui

    root = tk.Tk()
    root.withdraw()
    try:
        cfg = config.load()
        bubble = ui.BubbleWindow(root, cfg)
        bubble.show("hello world", "你好，世界\n第二行译文", is_error=False)
        root.update()
        bubble.show("hello world", "翻译中…", auto_hide=False)
        root.update()
        bubble.hide()

        # 长内容：限高 + 滚轮翻页
        long_text = "\n".join("第 %d 行 the quick brown fox" % i for i in range(150))
        cfg["backdrop"] = "gradient"
        bubble.show("hello world", long_text, auto_hide=False)
        root.update()
        if not bubble._scrollable:
            raise RuntimeError("长内容没有进入滚动模式")
        before = bubble._scroll
        bubble._on_wheel(types.SimpleNamespace(delta=-120))
        if bubble._scroll <= before:
            raise RuntimeError("滚轮没有翻动长内容")

        # 拖右下角：宽度 +60，内容重新换行
        width, height = bubble._size
        start = (bubble._pos[0] + width - 3, bubble._pos[1] + height - 3)
        bubble._on_press(
            types.SimpleNamespace(x=width - 3, y=height - 3, x_root=start[0], y_root=start[1])
        )
        bubble._on_motion(
            types.SimpleNamespace(
                x=width + 57, y=height - 3,
                x_root=start[0] + 60, y_root=start[1],
            )
        )
        bubble._apply_resize()
        root.update()
        if bubble._size[0] != width + 60:
            raise RuntimeError(f"拖右下角没有改变宽度：{bubble._size}")
        bubble._on_release(types.SimpleNamespace(x=0, y=0, x_root=start[0], y_root=start[1]))
        root.update()

        # 移动：拖动中背景保持不动（跟手重绘会卡），松手才重拍
        cfg["backdrop"] = "blur"
        cfg["backdrop_live_resize"] = True
        bubble.show("hello world", "你好，世界\n第二行译文", auto_hide=False)
        root.update()
        width, height = bubble._size
        origin = bubble._pos
        center = (origin[0] + width // 2, origin[1] + height // 2)
        before_photo = bubble._photo
        bubble._on_press(
            types.SimpleNamespace(x=width // 2, y=height // 2, x_root=center[0], y_root=center[1])
        )
        bubble._on_motion(
            types.SimpleNamespace(
                x=width // 2, y=height // 2,
                x_root=center[0] - 80, y_root=center[1] - 40,
            )
        )
        root.update()
        if bubble._photo is not before_photo:
            raise RuntimeError("移动过程中不该重绘背景")
        if bubble._drag_snapshot is not None:
            raise RuntimeError("移动过程中不该抓取背景快照")
        bubble._on_release(
            types.SimpleNamespace(x=0, y=0, x_root=center[0] - 80, y_root=center[1] - 40)
        )
        root.update()
        if bubble._photo is before_photo:
            raise RuntimeError("移动松手后没有重拍背景")

        # 缩放：拖动中也要有背景预览（别退回纯色），松手后释放快照
        width, height = bubble._size
        sx, sy = bubble._pos
        grip = (sx + width - 3, sy + height - 3)
        preview_before = bubble._photo
        bubble._on_press(
            types.SimpleNamespace(x=width - 3, y=height - 3, x_root=grip[0], y_root=grip[1])
        )
        bubble._on_motion(
            types.SimpleNamespace(
                x=width + 57, y=height + 37,
                x_root=grip[0] + 60, y_root=grip[1] + 40,
            )
        )
        bubble._apply_resize()
        root.update()
        if bubble._drag_snapshot is None:
            raise RuntimeError("缩放时没有抓到背景快照")
        if bubble._photo is preview_before:
            raise RuntimeError("缩放预览没有生成背景")
        if not bubble._canvas.find_withtag("bg"):
            raise RuntimeError("缩放中背景图元丢失")
        bubble._on_release(
            types.SimpleNamespace(x=0, y=0, x_root=grip[0] + 60, y_root=grip[1] + 40)
        )
        root.update()
        if bubble._drag_snapshot is not None:
            raise RuntimeError("缩放松手后快照没有释放")
        bubble.hide()

        prompt = ui.SelectionPrompt(root, lambda: None)
        prompt.show(*desktop.cursor_pos())
        root.update()
        if prompt._rect is None or not prompt._win.winfo_viewable():
            raise RuntimeError("划词按钮没有摆到鼠标旁边")
        rx, ry, rw, rh = prompt._rect
        if not prompt.hit(rx + rw // 2, ry + rh // 2):
            raise RuntimeError("划词按钮点击区域异常")
        prompt.hide()
    finally:
        root.destroy()
    return "气泡与划词按钮显示正常，移动不重绘、缩放用快照预览"


def _icon():
    import tray

    image = tray.draw_icon(64)
    return f"生成 {image.size[0]}x{image.size[1]} 图标"


def _autostart():
    import system

    return "已开启" if system.is_enabled() else "当前未开启（可正常读写注册表）"


def _mutex():
    import system

    return "获取成功" if system.acquire_single_instance() else "已有实例在运行"


if __name__ == "__main__":
    raise SystemExit(main())
