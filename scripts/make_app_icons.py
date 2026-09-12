"""生成 App 图标(2026-09-12 老板选定 B 方案: 柱状 + 上行箭头)。

单一几何源: 本脚本同时产出 `frontend/public/icon.svg` 与 `icon-192/512.png`,
避免矢量与位图各画各的。改配色/比例只改这里再跑一次:
    python scripts/make_app_icons.py
"""
from __future__ import annotations

import math
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PUB = os.path.join(ROOT, "frontend", "public")

SIZE = 512
TILE_R = 112
BG_TOP, BG_BOT = (25, 26, 53), (11, 11, 20)
BAR_FROM, BAR_TO = (79, 70, 229), (124, 58, 237)        # 柱体: indigo-600 → violet-600
ARROW_FROM, ARROW_TO = (99, 102, 241), (168, 85, 247)   # 折线/箭头: 更亮一档, 与柱体拉开层次
CASING = 12                                             # 折线外侧深色描边宽度(隔开柱体, 不糊成一团)

BARS = [                                                 # (x, 顶, 宽, 高) —— 三根递增柱, 底对齐
    (146, 296, 56, 108),
    (228, 246, 56, 158),
    (310, 208, 56, 196),
]
BAR_R = 28
ARROW_W = 36
ZIGZAG = [(118, 298), (184, 232), (226, 264), (394, 138)]   # 折线尾部→箭尖(箭尖在柱顶之上)


def arrow_head(tip: tuple[float, float], prev: tuple[float, float], length: float = 44, half: float = 34):
    """返回 (三角顶点, 底边中心) —— 折线收到"底边中心", 三角形补上箭尖。"""
    dx, dy = tip[0] - prev[0], tip[1] - prev[1]
    n = math.hypot(dx, dy)
    ux, uy = dx / n, dy / n
    base = (tip[0] - ux * length, tip[1] - uy * length)
    nx, ny = -uy, ux
    return tip, base, ((base[0] + nx * half, base[1] + ny * half),
                       (base[0] - nx * half, base[1] - ny * half))


TIP, BASE, WINGS = arrow_head(ZIGZAG[-1], ZIGZAG[-2])
LINE = ZIGZAG[:-1] + [BASE]


def svg() -> str:
    pts = " ".join(f"{x},{y}" for x, y in LINE)
    face = " ".join(f"{x:.1f},{y:.1f}" for x, y in (TIP, *WINGS))
    bars = "\n".join(
        f'  <rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{BAR_R}"/>' for x, y, w, h in BARS
    )
    hexs = lambda c: "#{:02x}{:02x}{:02x}".format(*c)  # noqa: E731
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {SIZE} {SIZE}" width="{SIZE}" height="{SIZE}">
  <defs>
    <linearGradient id="tile" x1="0" y1="0" x2="0.35" y2="1">
      <stop offset="0" stop-color="{hexs(BG_TOP)}"/>
      <stop offset="1" stop-color="{hexs(BG_BOT)}"/>
    </linearGradient>
    <linearGradient id="bar" x1="0.1" y1="0.1" x2="0.9" y2="1">
      <stop offset="0" stop-color="{hexs(BAR_FROM)}"/>
      <stop offset="1" stop-color="{hexs(BAR_TO)}"/>
    </linearGradient>
    <linearGradient id="arrow" x1="0.1" y1="0.1" x2="0.9" y2="1">
      <stop offset="0" stop-color="{hexs(ARROW_FROM)}"/>
      <stop offset="1" stop-color="{hexs(ARROW_TO)}"/>
    </linearGradient>
  </defs>
  <rect width="{SIZE}" height="{SIZE}" rx="{TILE_R}" fill="url(#tile)"/>
  <rect x="1.5" y="1.5" width="{SIZE - 3}" height="{SIZE - 3}" rx="{TILE_R - 1.5}"
        fill="none" stroke="#ffffff" stroke-opacity="0.07" stroke-width="3"/>
  <g fill="url(#bar)">
{bars}
  </g>
  <polyline points="{pts}" fill="none" stroke="{hexs(BG_BOT)}" stroke-width="{ARROW_W + CASING}"
            stroke-linecap="round" stroke-linejoin="round"/>
  <polyline points="{pts}" fill="none" stroke="url(#arrow)" stroke-width="{ARROW_W}"
            stroke-linecap="round" stroke-linejoin="round"/>
  <polygon points="{face}" fill="url(#arrow)"/>
</svg>
"""


def png(px: int, out: str) -> None:
    """用 Pillow 按同一几何栅格化(4x 超采样后缩放, 得到干净抗锯齿)。"""
    from PIL import Image, ImageDraw

    s = 4
    w = SIZE * s
    img = Image.new("RGB", (w, w), BG_BOT)
    d = ImageDraw.Draw(img)
    for y in range(w):                                   # 竖向渐变底
        t = y / (w - 1)
        d.line([(0, y), (w, y)], fill=tuple(round(BG_TOP[i] + (BG_BOT[i] - BG_TOP[i]) * t) for i in range(3)))

    def stroke(mask: Image.Image, width: float) -> None:  # 折线 + 各顶点圆帽
        m = ImageDraw.Draw(mask)
        m.line([(x * s, y * s) for x, y in LINE], fill=255, width=round(width * s), joint="curve")
        for x, y in LINE:
            r = width * s / 2
            m.ellipse([x * s - r, y * s - r, x * s + r, y * s + r], fill=255)

    bars = Image.new("L", (w, w), 0)
    b = ImageDraw.Draw(bars)
    for x, y, bw, bh in BARS:
        b.rounded_rectangle([x * s, y * s, (x + bw) * s, (y + bh) * s], radius=BAR_R * s, fill=255)

    casing = Image.new("L", (w, w), 0)
    stroke(casing, ARROW_W + CASING)

    arrow = Image.new("L", (w, w), 0)
    stroke(arrow, ARROW_W)
    ImageDraw.Draw(arrow).polygon([(x * s, y * s) for x, y in (TIP, *WINGS)], fill=255)

    mark = Image.composite(Image.new("L", (w, w), 0), bars, casing)     # 柱体挖掉折线走位
    mark = Image.composite(arrow, mark, arrow)

    grad = Image.new("RGB", (w, w))
    g = ImageDraw.Draw(grad)
    for i in range(w * 2):
        t = i / (w * 2 - 1)
        g.line([(i, 0), (0, i)], fill=tuple(round(BAR_FROM[k] + (ARROW_TO[k] - BAR_FROM[k]) * t) for k in range(3)))
    img.paste(grad, (0, 0), mark)

    tile = Image.new("L", (w, w), 0)                     # 圆角裁切
    ImageDraw.Draw(tile).rounded_rectangle([0, 0, w - 1, w - 1], radius=TILE_R * s, fill=255)
    out_img = Image.new("RGBA", (w, w), (0, 0, 0, 0))
    out_img.paste(img, (0, 0), tile)
    out_img.resize((px, px), Image.LANCZOS).save(out)


def main() -> None:
    with open(os.path.join(PUB, "icon.svg"), "w", encoding="utf-8", newline="\n") as f:
        f.write(svg())
    png(512, os.path.join(PUB, "icon-512.png"))
    png(192, os.path.join(PUB, "icon-192.png"))
    print("icon.svg / icon-512.png / icon-192.png 已更新")


if __name__ == "__main__":
    main()
