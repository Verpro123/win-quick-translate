# -*- coding: utf-8 -*-
"""气泡背景素材：预置渐变与自定义图片。

ui.py 只负责"把底图贴到卡片上"，这里只产出 RGB 底图：
    gradient()   按预设配色生成一张斜向渐变
    load_image() 把任意图片按"铺满裁切"的方式缩放到气泡尺寸

所有函数都不抛异常 —— 图片路径写错、文件损坏、PIL 不支持，一律返回 None，
由调用方退回渐变或纯色。背景坏了不能让气泡开不出来。
"""

from __future__ import annotations

import logging
from pathlib import Path

from PIL import Image, ImageChops, ImageFilter

log = logging.getLogger(__name__)

# 每套配色都从深排到浅：气泡左上暗、右下亮，正文压在上面也看得清
GRADIENT_PRESETS: dict[str, dict] = {
    "aurora": {"label": "极光", "colors": ["#0B1220", "#1E3A8A", "#7C3AED", "#F472B6"]},
    "ocean": {"label": "深海", "colors": ["#020617", "#0C4A6E", "#0EA5E9", "#67E8F9"]},
    "sunset": {"label": "日落", "colors": ["#1C1917", "#7C2D12", "#EA580C", "#FBBF24"]},
    "forest": {"label": "森林", "colors": ["#052E16", "#065F46", "#10B981", "#A7F3D0"]},
    "sakura": {"label": "樱粉", "colors": ["#3B0764", "#9D174D", "#EC4899", "#FBCFE8"]},
    "graphite": {"label": "石墨", "colors": ["#0F172A", "#1E293B", "#334155", "#94A3B8"]},
}

DEFAULT_GRADIENT = "aurora"


def preset_labels() -> dict[str, str]:
    """{显示名: 配置值}，给设置界面的下拉框用。"""
    return {spec["label"]: key for key, spec in GRADIENT_PRESETS.items()}


def preset_label(key: str) -> str:
    spec = GRADIENT_PRESETS.get(key) or GRADIENT_PRESETS[DEFAULT_GRADIENT]
    return spec["label"]


def preset_luminance(key: str) -> float:
    """某套配色的平均亮度（0-255），用来决定卡片上是深色字还是浅色字。"""
    spec = GRADIENT_PRESETS.get(key) or GRADIENT_PRESETS[DEFAULT_GRADIENT]
    values = [
        0.299 * red + 0.587 * green + 0.114 * blue
        for red, green, blue in (_rgb(color) for color in spec["colors"])
    ]
    return sum(values) / max(1, len(values))


def _rgb(color: str) -> tuple[int, int, int]:
    text = str(color).lstrip("#")
    if len(text) != 6:
        return (31, 32, 35)
    try:
        return (int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16))
    except ValueError:
        return (31, 32, 35)


def _stops_strip(colors: list[str], width: int) -> Image.Image:
    """把若干颜色均匀插值成一条 1×width 的色带。"""
    stops = [_rgb(color) for color in colors] or [(31, 32, 35)]
    if len(stops) == 1:
        stops = stops * 2
    span = len(stops) - 1
    strip = Image.new("RGB", (width, 1))
    pixels = strip.load()
    for x in range(width):
        t = x / max(1, width - 1) * span
        index = min(int(t), span - 1)
        fraction = t - index
        low, high = stops[index], stops[index + 1]
        pixels[x, 0] = tuple(
            round(low[channel] + (high[channel] - low[channel]) * fraction)
            for channel in range(3)
        )
    return strip


_RAMP: Image.Image | None = None


def _diagonal_ramp() -> Image.Image:
    """一张 0→255 的斜向灰度图（左上黑、右下白），只生成一次。"""
    global _RAMP
    if _RAMP is None:
        vertical = Image.linear_gradient("L")           # 上黑下白
        horizontal = vertical.rotate(90, expand=True)   # 左黑右白
        _RAMP = ImageChops.add(vertical, horizontal, scale=2.0)
    return _RAMP


def gradient(size: tuple[int, int], preset: str = DEFAULT_GRADIENT) -> Image.Image:
    """按灰度斜坡查色带，得到一张平滑的斜向多段渐变。

    走的是「索引图 + 三张查找表」，不逐像素算颜色，600×900 也就几毫秒。
    """
    width, height = max(1, int(size[0])), max(1, int(size[1]))
    spec = GRADIENT_PRESETS.get(preset) or GRADIENT_PRESETS[DEFAULT_GRADIENT]
    strip = _stops_strip(spec["colors"], 256)
    pixels = list(strip.getdata())
    lut_r = [pixel[0] for pixel in pixels]
    lut_g = [pixel[1] for pixel in pixels]
    lut_b = [pixel[2] for pixel in pixels]

    ramp = _diagonal_ramp().resize((width, height), Image.BILINEAR)
    return Image.merge(
        "RGB",
        (ramp.point(lut_r), ramp.point(lut_g), ramp.point(lut_b)),
    )


def cover(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    """等比缩放后居中裁切，铺满目标尺寸（不留黑边）。"""
    width, height = max(1, int(size[0])), max(1, int(size[1]))
    source_w, source_h = image.size
    if source_w <= 0 or source_h <= 0:
        return Image.new("RGB", (width, height), (31, 32, 35))
    scale = max(width / source_w, height / source_h)
    resized = image.resize(
        (max(1, round(source_w * scale)), max(1, round(source_h * scale))),
        Image.LANCZOS,
    )
    left = max(0, (resized.width - width) // 2)
    top = max(0, (resized.height - height) // 2)
    return resized.crop((left, top, left + width, top + height))


def image_luminance(path: str | Path) -> float | None:
    """图片的平均亮度（0-255）；读不出来返回 None。

    缩到 48 像素再算，几十微秒就能出结果 —— 只用来判断该配浅色字还是深色字。
    """
    text = str(path or "").strip()
    if not text:
        return None
    try:
        with Image.open(text) as source:
            grey = source.convert("L")
            grey.thumbnail((48, 48))
            histogram = grey.histogram()
    except Exception:  # noqa: BLE001
        return None
    total = sum(histogram)
    if not total:
        return None
    return sum(value * count for value, count in enumerate(histogram)) / total


def load_image(
    path: str | Path, size: tuple[int, int], blur: float = 0.0
) -> Image.Image | None:
    """读一张图片并铺满目标尺寸；读不出来返回 None。"""
    text = str(path or "").strip()
    if not text:
        return None
    try:
        with Image.open(text) as source:
            image = source.convert("RGB")
    except Exception as exc:  # noqa: BLE001
        log.warning("背景图片读取失败（%s）：%s", text, exc)
        return None
    image = cover(image, size)
    if blur > 0:
        image = image.filter(ImageFilter.GaussianBlur(blur))
    return image
