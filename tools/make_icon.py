# -*- coding: utf-8 -*-
"""生成 AIOps 平台 logo.ico（与前端 logo.svg 一致的六边形拓扑图形）。

用于 PyInstaller exe 图标与 Windows 开始菜单快捷方式图标。
"""
import os
from PIL import Image, ImageDraw

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "build", "logo.ico")


def _pt(x, y, scale):
    return (x * scale, y * scale)


def draw_logo(img: Image.Image, scale: float) -> None:
    d = ImageDraw.Draw(img)

    def w(v: float) -> int:
        return max(1, int(round(v * scale)))

    # 六边形外框
    hexagon = [_pt(x, y, scale) for x, y in
               [(32, 5), (54.5, 18.5), (54.5, 45.5), (32, 59), (9.5, 45.5), (9.5, 18.5)]]
    d.polygon(hexagon, fill="#0e4a5e", outline="#22d3ee", width=w(2.5))

    # 中心到四个节点的连线
    for x2, y2 in [(23, 23), (41, 23), (41, 41), (23, 41)]:
        d.line([_pt(32, 32, scale), _pt(x2, y2, scale)], fill="#22d3ee", width=w(1))

    # 中心节点
    cx, cy = 32 * scale, 32 * scale
    r = 5 * scale
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill="#22d3ee")

    # 四个边缘节点
    for nx, ny in [(23, 23), (41, 23), (41, 41), (23, 41)]:
        nx2, ny2 = nx * scale, ny * scale
        r2 = 3 * scale
        d.ellipse([nx2 - r2, ny2 - r2, nx2 + r2, ny2 + r2], fill="#67e8f9")

    # 底部折线
    poly = [(16, 48), (25, 48), (31, 44), (35, 52), (41, 47), (48, 48)]
    d.line([_pt(x, y, scale) for x, y in poly], fill="#22d3ee", width=w(2.2), joint="curve")


def main() -> None:
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    base = 256
    img = Image.new("RGBA", (base, base), (0, 0, 0, 0))
    draw_logo(img, base / 64.0)
    sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    img.save(OUT, format="ICO", sizes=sizes)
    print("已生成", os.path.abspath(OUT))


if __name__ == "__main__":
    main()
