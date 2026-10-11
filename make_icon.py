# -*- coding: utf-8 -*-
"""生成 assets/icon.ico（打包 exe 时作为程序图标）。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from PIL import Image  # noqa: E402

from tray import draw_icon  # noqa: E402


def main() -> int:
    target = ROOT / "assets" / "icon.ico"
    target.parent.mkdir(parents=True, exist_ok=True)
    image = draw_icon(256)
    image.save(
        target,
        format="ICO",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )
    print(f"图标已生成：{target}  尺寸 {image.size}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
