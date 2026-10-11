# -*- coding: utf-8 -*-
"""系统托盘图标与右键菜单。

托盘菜单的回调跑在 pystray 自己的线程里，所以这里**只投递动作**，
一律通过 app.submit() 转交给 tkinter 主线程执行。
"""

from __future__ import annotations

import logging
from pathlib import Path

import pystray
from PIL import Image, ImageDraw, ImageFont

import config as config_mod

log = logging.getLogger(__name__)

ICON_BG = (37, 99, 235, 255)      # 蓝底
ICON_FG = (255, 255, 255, 255)    # 白字


def load_icon_image(size: int = 64) -> Image.Image:
    """优先用随包分发的 ico；没有就现画一个。"""
    ico_path = config_mod.resource_path("assets/icon.ico")
    if ico_path.exists():
        try:
            return Image.open(ico_path).convert("RGBA")
        except Exception as exc:  # noqa: BLE001
            log.debug("读取图标失败，改用内置图形：%s", exc)
    return draw_icon(size)


def draw_icon(size: int = 64) -> Image.Image:
    """画一个「译」字图标；拿不到中文字体就退化成字母 A。"""
    scale = 4  # 先大后缩，边缘更干净
    big = size * scale
    image = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    draw.rounded_rectangle(
        [0, 0, big - 1, big - 1], radius=int(big * 0.22), fill=ICON_BG
    )

    font = _pick_font(int(big * 0.62))
    text = "译" if font else "A"
    try:
        draw.text((big / 2, big / 2 + big * 0.02), text, font=font, fill=ICON_FG, anchor="mm")
    except Exception:  # noqa: BLE001
        draw.text((big / 2, big / 2), "A", fill=ICON_FG, anchor="mm")

    return image.resize((size, size), Image.LANCZOS)


def _pick_font(size: int):
    """找一个能画中文的字体。"""
    candidates = [
        r"C:\Windows\Fonts\msyhbd.ttc",
        r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\simhei.ttf",
    ]
    for path in candidates:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return None


class TrayIcon:
    """托盘图标本体。"""

    def __init__(self, app) -> None:
        self.app = app
        self.icon = pystray.Icon(
            "QuickTranslate",
            icon=load_icon_image(64),
            title=f"{config_mod.APP_TITLE}（{app.cfg.get('hotkey', '')}）",
            menu=self._build_menu(),
        )

    def _build_menu(self) -> pystray.Menu:
        cfg = lambda: self.app.cfg  # noqa: E731 - 菜单回调里要读最新配置

        return pystray.Menu(
            pystray.MenuItem(
                lambda _i: f"翻译选中内容（{cfg().get('hotkey', '')}）",
                lambda: self.app.submit("translate"),
                default=True,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "百度翻译",
                lambda: self.app.submit("set_engine", "baidu"),
                radio=True,
                checked=lambda _i: cfg().get("engine") == "baidu",
            ),
            pystray.MenuItem(
                "AI 大模型",
                lambda: self.app.submit("set_engine", "ai"),
                radio=True,
                checked=lambda _i: cfg().get("engine") == "ai",
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("设置…", lambda: self.app.submit("settings")),
            pystray.MenuItem(
                "开机自动启动",
                lambda: self.app.submit("toggle_autostart"),
                checked=lambda _i: bool(cfg().get("autostart")),
            ),
            pystray.MenuItem("打开配置目录", lambda: self.app.submit("open_dir")),
            pystray.MenuItem("使用方法", lambda: self.app.submit("help")),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", lambda: self.app.submit("quit")),
        )

    # ---- 生命周期 ----

    def run(self) -> None:
        try:
            self.icon.run()
        except Exception:
            log.exception("托盘图标启动失败")

    def stop(self) -> None:
        try:
            self.icon.stop()
        except Exception:  # noqa: BLE001
            log.debug("停止托盘图标时出现异常，忽略")

    def notify(self, message: str, title: str | None = None) -> None:
        """弹一个系统通知（气泡提示）。"""
        try:
            self.icon.notify(message, title or config_mod.APP_TITLE)
        except Exception as exc:  # noqa: BLE001
            log.debug("通知发送失败：%s", exc)

    def refresh_tooltip(self, hotkey: str) -> None:
        try:
            self.icon.title = f"{config_mod.APP_TITLE}（{hotkey}）"
        except Exception:  # noqa: BLE001
            pass
