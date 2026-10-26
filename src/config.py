# -*- coding: utf-8 -*-
"""配置与路径管理。

配置以 JSON 形式保存，默认放在程序同目录（便携，方便直接用记事本改）；
若该目录不可写（比如装到了 Program Files），自动回退到 %APPDATA%\\QuickTranslate。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

APP_NAME = "QuickTranslate"
APP_TITLE = "划词翻译"
APP_VERSION = "1.1.1"

# ---------------------------------------------------------------- 默认配置

DEFAULT_CONFIG: dict[str, Any] = {
    # 触发翻译的全局快捷键，格式参考 keyboard 库：ctrl+alt+t / alt+q / ctrl+shift+d
    "hotkey": "ctrl+alt+t",
    # 当前使用的引擎：baidu | ai
    "engine": "baidu",
    # 气泡背景：blur = 抓屏做高斯模糊（毛玻璃），solid = 纯色卡片
    "backdrop": "blur",
    # 气泡显示时长（秒），0 表示不自动关闭
    "bubble_timeout": 7.0,
    # 气泡宽度上限（像素）
    "bubble_width": 520,
    # 气泡正文字号
    "font_size": 11,
    # 选中文字后是否在鼠标旁弹出「翻译」小按钮
    "selection_button": True,
    # 模拟复制后等待剪贴板更新的最长时间（秒）
    "capture_timeout": 0.6,
    # 允许翻译的最大字符数，超出则截断（防止一整篇文章糊满屏幕）
    "max_chars": 1200,
    # 是否把译文同时写回剪贴板
    "copy_result": False,
    # AI 引擎请求超时（秒）
    "ai_timeout": 25,
    "baidu": {
        "appid": "",
        "secret": "",
    },
    "ai": {
        # 任何 OpenAI 兼容接口都可以，路径会自动补 /chat/completions
        "base_url": "https://api.deepseek.com",
        "api_key": "",
        "model": "deepseek-chat",
        # 留空使用内置提示词
        "prompt": "",
    },
    "autostart": False,
}

# 常见服务商预设，设置界面里一键填充
AI_PRESETS: dict[str, dict[str, str]] = {
    "DeepSeek": {
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-chat",
    },
    "通义千问": {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-plus",
    },
    "智谱 GLM": {
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4-flash",
    },
    "Kimi": {
        "base_url": "https://api.moonshot.cn/v1",
        "model": "moonshot-v1-8k",
    },
    "本地 Ollama": {
        "base_url": "http://127.0.0.1:11434/v1",
        "model": "qwen2.5:7b",
    },
}

# ---------------------------------------------------------------- 路径解析


def app_dir() -> Path:
    """程序所在目录。打包成 exe 后是 exe 所在目录，源码运行时是 src 的上级。"""
    import sys

    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def resource_path(relative: str) -> Path:
    """取随程序分发的资源（图标等）。打包后资源被解压到 sys._MEIPASS。"""
    import sys

    base = getattr(sys, "_MEIPASS", None)
    if base:
        return Path(base) / relative
    return app_dir() / relative


def _writable(directory: Path) -> bool:
    """真正写一个探针文件来判断目录是否可写（Windows 上 os.access 不可靠）。"""
    probe = directory / f".qt_probe_{os.getpid()}"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def config_path() -> Path:
    primary = app_dir()
    if _writable(primary):
        return primary / "config.json"
    fallback = Path(os.environ.get("APPDATA") or Path.home()) / APP_NAME
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback / "config.json"


def log_path() -> Path:
    return config_path().parent / "quicktranslate.log"


# ---------------------------------------------------------------- 读写


def _deep_merge(base: dict, override: dict) -> dict:
    """用 override 覆盖 base，但保留 base 里 override 缺失的键（支持嵌套）。"""
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load() -> dict[str, Any]:
    """读取配置。文件缺失或损坏时回退到默认值，并把损坏文件留档备查。"""
    path = config_path()
    data: dict[str, Any] = {}
    if path.exists():
        try:
            raw = path.read_text(encoding="utf-8")
            parsed = json.loads(raw) if raw.strip() else {}
            if isinstance(parsed, dict):
                data = parsed
        except (OSError, json.JSONDecodeError) as exc:
            logging.warning("配置文件解析失败，已重置：%s", exc)
            try:
                shutil.copy2(path, path.with_suffix(".json.broken"))
            except OSError:
                pass
    return _deep_merge(DEFAULT_CONFIG, data)


def save(cfg: dict[str, Any]) -> None:
    """原子写入配置，避免中途断电留下半个文件。"""
    path = config_path()
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------- 日志


def setup_logging(level: int = logging.INFO) -> None:
    """打包成 --noconsole 的 exe 后没有 stdout，所以日志一律落盘。"""
    import sys

    handlers: list[logging.Handler] = []
    try:
        handlers.append(
            RotatingFileHandler(
                log_path(), maxBytes=512 * 1024, backupCount=2, encoding="utf-8"
            )
        )
    except OSError:
        pass
    # 只有源码方式运行（还有 stderr）时才往控制台也打一份
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler(sys.stderr))

    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=handlers or [logging.NullHandler()],
        force=True,
    )
