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
    return (
        f"鼠标 ({x},{y}) 在当前屏 {right - left}×{bottom - top} 内；"
        f"虚拟屏幕 {vw}×{vh} @ ({vleft},{vtop})"
    )


def _bubble():
    import tkinter as tk

    import config
    import desktop
    import ui

    root = tk.Tk()
    root.withdraw()
    try:
        bubble = ui.BubbleWindow(root, config.load())
        bubble.show("hello world", "你好，世界\n第二行译文", is_error=False)
        root.update()
        bubble.show("hello world", "翻译中…", auto_hide=False)
        root.update()
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
    return "气泡与划词按钮显示正常"


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
