# -*- coding: utf-8 -*-
"""一键打包成单个 exe。

用法：
    python build.py

产物：dist/QuickTranslate.exe
把这个 exe 放到任意固定目录（例如 C:\\Tools\\QuickTranslate\\），
再在托盘菜单里勾上「开机自动启动」就行。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# 这些库万一被间接牵连进来，会把 exe 撑大好几倍，明确排掉
EXCLUDES = [
    "numpy", "pandas", "matplotlib", "scipy", "PyQt5", "PyQt6",
    "PySide2", "PySide6", "IPython", "pytest", "setuptools",
    "pip", "docutils", "sqlite3", "tkinter.test", "test",
]


def main() -> int:
    icon = ROOT / "assets" / "icon.ico"
    if not icon.exists():
        subprocess.run([sys.executable, str(ROOT / "make_icon.py")], check=True)

    args = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--noconsole",                     # 不要黑框
        "--name", "QuickTranslate",
        "--icon", str(icon),
        "--paths", str(ROOT / "src"),
        "--add-data", f"{icon}{os.pathsep}assets",
        "--distpath", str(ROOT / "dist"),
        "--workpath", str(ROOT / "build"),
        "--specpath", str(ROOT),
    ]
    for name in EXCLUDES:
        args += ["--exclude-module", name]
    args.append(str(ROOT / "main.py"))

    print("正在打包，第一次会比较慢（PyInstaller 要分析整个依赖树）…")
    result = subprocess.run(args, cwd=ROOT)
    if result.returncode != 0:
        print("打包失败，请看上面的报错。")
        return result.returncode

    exe = ROOT / "dist" / "QuickTranslate.exe"
    if not exe.exists():
        print("打包命令返回成功，但没找到 exe，请检查 dist 目录。")
        return 1

    # 复制一份到项目根目录，双击就能用，不用去 dist 里翻
    root_copy = ROOT / "QuickTranslate.exe"
    try:
        shutil.copy2(exe, root_copy)
    except OSError:
        root_copy = exe

    size_mb = root_copy.stat().st_size / 1024 / 1024
    print(f"\n打包完成：{root_copy}")
    print(f"体积：{size_mb:.1f} MB")

    # 中间产物留着没用，顺手清掉
    for junk in (ROOT / "build", ROOT / "QuickTranslate.spec"):
        try:
            if junk.is_dir():
                shutil.rmtree(junk)
            elif junk.exists():
                junk.unlink()
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
