# -*- coding: utf-8 -*-
"""和 Windows 系统打交道的两件事：全局热键、开机自启动。

关于权限：开机自启动写在 HKCU（当前用户）下，不需要管理员权限，
也不会触发 UAC 弹窗。代价是只对当前用户生效 —— 对一个个人小工具来说正合适。
"""

from __future__ import annotations

import logging
import re
import sys
from pathlib import Path

import keyboard

log = logging.getLogger(__name__)

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "QuickTranslate"

_MODIFIER_NAMES = {"ctrl", "control", "alt", "shift", "windows", "win", "cmd"}


def hotkey_advice(combo: str) -> str:
    """对某个快捷键给一句知情提示。**不阻止使用**，只把后果说清楚。

    这里是刻意不做拦截的：快捷键是用户自己机器上的事，拦着不让设属于多管闲事。
    但有两种情况值得顺嘴提一句 ——
      · Ctrl + 单个字母（比如 Ctrl+C）：系统自己也占着，绑上去之后
        日常复制粘贴也会顺手触发翻译；
      · 没有修饰键：会和正常打字撞车。
    听不听，用户自己定。
    """
    combo = (combo or "").strip().lower()
    if not combo:
        return ""

    parts = [p for p in re.split(r"[+\-]", combo) if p]
    if not parts:
        return ""

    modifiers = [p for p in parts if p in _MODIFIER_NAMES]
    others = [p for p in parts if p not in _MODIFIER_NAMES]

    if not modifiers:
        if len(others) == 1 and re.fullmatch(r"f\d{1,2}", others[0]):
            return ""
        return "（提醒：没有修饰键，打字时容易误触发）"

    if not others:
        return ""

    if any(m in ("windows", "win", "cmd") for m in modifiers):
        return "（提醒：Win 组合归系统优先处理，可能抢不到）"

    if len(parts) == 2 and len(others[0]) == 1 and "ctrl" in modifiers:
        return f"（提醒：{combo} 系统本身也在用，之后每次复制粘贴都会弹出翻译）"

    return ""


# ---------------------------------------------------------------- 全局热键


class HotkeyManager:
    """注册和更换全局热键。

    注意：回调是在 keyboard 库的按键监听线程里同步执行的，
    所以回调必须「立刻返回」—— 实际工作请丢进队列由别处处理。
    """

    def __init__(self, callback) -> None:
        self._callback = callback
        self._handle = None
        self._combo: str | None = None

    @property
    def combo(self) -> str | None:
        return self._combo

    def register(self, combo: str) -> str:
        """换一个热键。

        故意「先注册成功、再撤旧的」—— 新组合被占用时直接抛异常，
        旧热键原封不动继续可用，不会出现两个都没绑上的尴尬局面。
        """
        combo = (combo or "").strip().lower()
        if not combo:
            raise ValueError("快捷键不能为空")

        handle = keyboard.add_hotkey(combo, self._callback, suppress=False)
        self.unregister()
        self._handle = handle
        self._combo = combo
        log.info("已注册热键：%s", combo)
        return combo

    def unregister(self) -> None:
        if self._handle is None:
            return
        try:
            keyboard.remove_hotkey(self._handle)
        except (KeyError, ValueError) as exc:
            log.debug("移除热键时忽略异常：%s", exc)
        self._handle = None
        self._combo = None


# ---------------------------------------------------------------- 开机自启动


def launch_command() -> str:
    """返回写进注册表的启动命令。

    打包成 exe 时直接启动 exe；源码运行时用 pythonw.exe 拉起来（不带黑框）。
    """
    if getattr(sys, "frozen", False):
        return f'"{Path(sys.executable).resolve()}"'

    script = Path(__file__).resolve().parent.parent / "main.py"
    interpreter = Path(sys.executable).resolve()
    pythonw = interpreter.with_name("pythonw.exe")
    if pythonw.exists():
        interpreter = pythonw
    return f'"{interpreter}" "{script}"'


def is_enabled() -> bool:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as key:
            value, _ = winreg.QueryValueEx(key, VALUE_NAME)
            return bool(value)
    except OSError:
        return False


def set_enabled(enabled: bool) -> None:
    """开关自启动。会顺手把命令更新成当前路径，方便挪动 exe 后自愈。"""
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, launch_command())
            log.info("已写入开机自启动：%s", launch_command())
        else:
            try:
                winreg.DeleteValue(key, VALUE_NAME)
                log.info("已移除开机自启动")
            except FileNotFoundError:
                pass


# ---------------------------------------------------------------- 单实例


def acquire_single_instance() -> bool:
    """防止开两个进程抢同一个热键。返回 True 表示本进程是唯一实例。"""
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]

    ERROR_ALREADY_EXISTS = 183
    kernel32.CreateMutexW(None, False, "QuickTranslate_SingleInstance")
    return ctypes.get_last_error() != ERROR_ALREADY_EXISTS
