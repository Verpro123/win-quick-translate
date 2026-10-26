# -*- coding: utf-8 -*-
"""划词翻译 —— 程序入口与主循环。

线程模型（tkinter 不是线程安全的，所以边界划得很死）：
    主线程      tkinter 消息循环 —— 所有界面操作都在这里
    keyboard    全局热键回调 —— 只往队列丢一条任务就立刻返回，
                否则会堵住键盘监听线程，导致按键丢失
    pystray     托盘图标消息循环
    worker      每次翻译临时起一个线程，跑完即退

跨线程一律走 self.queue，由主线程的 _pump 统一处理。
"""

from __future__ import annotations

import logging
import os
import queue
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox

_HERE = Path(__file__).resolve().parent
if (_HERE / "src").is_dir() and str(_HERE / "src") not in sys.path:
    sys.path.insert(0, str(_HERE / "src"))

import capture  # noqa: E402
import config as config_mod  # noqa: E402
import desktop  # noqa: E402
import engines  # noqa: E402
import system  # noqa: E402
import tray as tray_mod  # noqa: E402
import ui  # noqa: E402

log = logging.getLogger(__name__)

HELP_TEXT = """使用方法

1. 用鼠标选中任意一段英文（单词、短语、整句都行）
2. 松开鼠标，选中的文字旁边会冒出「翻译」小按钮，点一下就能译
   也可以直接按快捷键（默认 ctrl+alt+t，可在设置里改）
3. 译文气泡会跟着鼠标弹出，接几块屏都一样

几个小技巧
· 气泡可以按住拖动，松手后会按新位置重新取背景
· 点击气泡可以立刻关掉它
· 鼠标停在气泡上的时候，它不会自动消失
· 气泡会同时显示原文和译文，方便对照
· 不想看到划词小按钮，去设置里关掉即可

第一次使用
· 打开托盘的「设置…」，填上百度翻译的 APPID 与密钥，
  或者切到 AI 大模型，填一个 API Key（DeepSeek、通义、智谱等都可以）
· 想让它开机自己跑起来，勾上设置里的「开机自动启动」

遇到问题
· 翻译没反应？先确认有没有选中文字，再确认密钥是否配置正确
· 日志文件就在配置文件旁边，叫 quicktranslate.log
"""


class App:
    def __init__(self) -> None:
        self.cfg = config_mod.load()
        self.queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self._token = 0
        self._busy = False

        self.root = tk.Tk()
        self.root.withdraw()          # 只借它的消息循环，主窗口永远不露脸
        self.root.title(config_mod.APP_TITLE)

        self.bubble = ui.BubbleWindow(self.root, self.cfg)
        self.prompt = ui.SelectionPrompt(self.root, lambda: self.submit("translate"))
        self.settings = ui.SettingsWindow(self.root, self.cfg, on_save=self.apply_settings)
        self.hotkeys = system.HotkeyManager(lambda: self.submit("translate"))
        self.tray = tray_mod.TrayIcon(self)
        self.mouse = desktop.SelectionWatcher(
            on_select=lambda *pos: self.submit("selection_prompt", pos),
            on_click=lambda x, y: self.submit("mouse_down", (x, y)),
        )

    # ------------------------------------------------------------ 启动

    def run(self) -> None:
        try:
            self.hotkeys.register(self.cfg["hotkey"])
        except Exception as exc:  # noqa: BLE001
            log.error("热键注册失败：%s", exc)
            self.root.after(
                1200,
                lambda: self.tray.notify(
                    f"快捷键 {self.cfg['hotkey']} 注册失败：{exc}\n"
                    "可能被别的程序占用了，去设置里换一个。",
                    "热键注册失败",
                ),
            )

        threading.Thread(target=self.tray.run, name="tray", daemon=True).start()
        self.mouse.start()
        self.root.after(60, self._pump)
        self.root.after(2000, self._first_run_check)
        log.info("启动完成，日志位置：%s", config_mod.log_path())
        self.root.mainloop()

    # 热键一律按用户配置原样注册，不做任何自动修改。
    # 各种组合的利弊由设置窗口以「提醒」形式告知，改不改是用户自己的事。

    def _first_run_check(self) -> None:
        """没配置引擎的话，直接把设置窗口推到用户脸上。"""
        engine = engines.build_engine(self.cfg)
        ok, reason = engine.ready()
        if ok:
            return
        self.tray.notify(f"还差一步：{reason}\n在弹出来的设置窗口里填一下就好", "先配置一下")
        self.settings.open()

    # ------------------------------------------------------------ 主线程泵

    def _pump(self) -> None:
        try:
            while True:
                action, payload = self.queue.get_nowait()
                try:
                    self._handle(action, payload)
                except Exception:  # noqa: BLE001
                    log.exception("处理动作 %s 时出错", action)
        except queue.Empty:
            pass
        finally:
            self.root.after(60, self._pump)

    def submit(self, action: str, payload: object = None) -> None:
        """任何线程都可以调用，把动作投递给主线程。"""
        self.queue.put((action, payload))

    def _handle(self, action: str, payload: object) -> None:
        if action == "translate":
            self._start_translate()
        elif action == "bubble":
            source, body, is_error, auto_hide = payload  # type: ignore[misc]
            self.bubble.show(source, body, is_error=is_error, auto_hide=auto_hide)
        elif action == "selection_prompt":
            self._show_prompt(*payload)  # type: ignore[misc]
        elif action == "mouse_down":
            x, y = payload  # type: ignore[misc]
            if not self.prompt.hit(x, y):
                self.prompt.hide()
        elif action == "settings":
            self.settings.open()
        elif action == "set_engine":
            self._set_engine(str(payload))
        elif action == "toggle_autostart":
            self._toggle_autostart()
        elif action == "open_dir":
            os.startfile(config_mod.config_path().parent)  # noqa: S606
        elif action == "help":
            self._show_help()
        elif action == "quit":
            self.quit()
        else:
            log.warning("未知动作：%s", action)

    # ------------------------------------------------------------ 翻译流程

    def _start_translate(self) -> None:
        self.prompt.hide()
        if self._busy:
            log.info("上一次翻译还没结束，忽略这次触发")
            return
        self._busy = True
        self._token += 1
        threading.Thread(
            target=self._translate_worker, args=(self._token,), name="translate", daemon=True
        ).start()

    def _translate_worker(self, token: int) -> None:
        try:
            engine = engines.build_engine(self.cfg)
            ok, reason = engine.ready()
            if not ok:
                self._bubble("", f"还没配置好翻译引擎：{reason}\n右键托盘图标 → 设置", True)
                return

            # 取词
            try:
                text = capture.grab_selection(
                    timeout=float(self.cfg.get("capture_timeout", 0.6))
                )
            except capture.CaptureError as exc:
                self._bubble("", str(exc), True)
                return
            except Exception as exc:  # noqa: BLE001
                log.exception("取词异常")
                self._bubble("", f"取词失败：{exc}", True)
                return

            limit = int(self.cfg.get("max_chars", 1200))
            if len(text) > limit:
                text = text[:limit].rstrip() + " …"

            if not engines.looks_like_english(text):
                self._bubble(text, "这段内容里没有英文字母，换个地方再试试", True)
                return

            # 先给个即时反馈，别让用户对着屏幕干等
            self._bubble(text, "翻译中…", False, auto_hide=False)

            try:
                result = engine.translate(text)
            except engines.TranslateError as exc:
                self._bubble(text, str(exc), True)
                return
            except Exception as exc:  # noqa: BLE001
                log.exception("翻译异常")
                self._bubble(text, f"翻译失败：{exc}", True)
                return

            if token != self._token:
                return  # 期间又触发了新的一次，这次结果作废

            self._bubble(text, result, False)

            if self.cfg.get("copy_result"):
                capture.copy_to_clipboard(result.text)
        finally:
            self._busy = False

    def _bubble(self, source: str, body: str, is_error: bool, auto_hide: bool = True) -> None:
        self.queue.put(("bubble", (source, body, is_error, auto_hide)))

    def _show_prompt(self, x: int, y: int, press_hwnd: int) -> None:
        """鼠标划选结束后，在松开的位置旁边放一个「翻译」按钮。"""
        if not self.cfg.get("selection_button", True):
            return
        # 从我们自己的窗口上开始拖的（气泡、设置、按钮）不算划词。
        # 用按下时的窗口句柄判断：气泡可能已经被拖到别处了，比坐标可靠。
        if self._is_own_window(press_hwnd):
            return
        self.prompt.show(x, y)

    def _is_own_window(self, hwnd: int) -> bool:
        if not hwnd:
            return False
        return hwnd in (
            self.bubble.hwnd(),
            self.prompt.hwnd(),
            self.settings.hwnd(),
        )

    # ------------------------------------------------------------ 设置

    def apply_settings(self, new_cfg: dict) -> None:
        """设置窗口点保存时回调。这里抛出的异常会被窗口显示出来。"""
        if new_cfg.get("hotkey") != self.cfg.get("hotkey"):
            # 注册失败会在保存之前抛出，配置不会被写坏
            self.hotkeys.register(new_cfg["hotkey"])
            self.tray.refresh_tooltip(new_cfg["hotkey"])

        if bool(new_cfg.get("autostart")) != bool(self.cfg.get("autostart")):
            try:
                system.set_enabled(bool(new_cfg.get("autostart")))
            except OSError as exc:
                raise RuntimeError(f"写入开机自启动失败：{exc}") from exc

        config_mod.save(new_cfg)
        self.cfg = new_cfg
        self.bubble.cfg = new_cfg
        self.settings.cfg = new_cfg
        if not new_cfg.get("selection_button", True):
            self.prompt.hide()
        log.info("配置已更新：引擎=%s 热键=%s", new_cfg.get("engine"), new_cfg.get("hotkey"))

    def _set_engine(self, name: str) -> None:
        self.cfg["engine"] = name
        config_mod.save(self.cfg)
        self.settings.engine_var.set(name)

        engine = engines.build_engine(self.cfg)
        ok, reason = engine.ready()
        label = "百度翻译" if name == "baidu" else "AI 大模型"
        if ok:
            self.tray.notify(f"已切换到{label}")
        else:
            self.tray.notify(f"已切换到{label}，但它还缺配置：{reason}")
            self.settings.open()

    def _toggle_autostart(self) -> None:
        want = not bool(self.cfg.get("autostart"))
        try:
            system.set_enabled(want)
        except OSError as exc:
            self.tray.notify(f"设置开机自启动失败：{exc}")
            return
        self.cfg["autostart"] = want
        config_mod.save(self.cfg)
        self.settings.autostart_var.set(want)
        self.tray.notify("已开启开机自动启动" if want else "已关闭开机自动启动")

    def _show_help(self) -> None:
        try:
            messagebox.showinfo(f"{config_mod.APP_TITLE} · 使用说明", HELP_TEXT)
        except Exception:  # noqa: BLE001
            log.exception("显示帮助失败")

    # ------------------------------------------------------------ 退出

    def quit(self) -> None:
        log.info("正在退出")
        try:
            self.mouse.stop()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.hotkeys.unregister()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.tray.stop()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.root.quit()
            self.root.destroy()
        except Exception:  # noqa: BLE001
            pass


def main() -> int:
    config_mod.setup_logging()
    if not system.acquire_single_instance():
        log.warning("已经有一个实例在跑了，本次启动直接退出")
        return 0
    App().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
