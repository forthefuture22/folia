#!/usr/bin/env python3
"""把整页截图裁切并放大，方便看清细节。"""
import sys
from PIL import Image

src = sys.argv[1]
img = Image.open(src)
print("size", img.size)

# 参数：name x y w h  scale  （坐标按 CSS px，脚本自动 ×2）
CROPS = [
    ("top_left",    0,   0, 380, 100, 3),
    ("top_center", 370,  40, 420,  50, 4),
    ("top_right",  700,  40, 390,  50, 4),
    ("rail",         0,  90,  70, 700, 3),
    ("side_head",   45,  95, 210,  60, 4),
    ("thumbs",      50, 140, 200, 380, 3),
    ("page_top",   310, 100, 710, 200, 2),
]

for name, x, y, w, h, s in CROPS:
    box = (x * 2, y * 2, (x + w) * 2, (y + h) * 2)
    c = img.crop(box)
    c = c.resize((c.width * s // 2 * 1, c.height * s // 2 * 1), Image.LANCZOS) if False else c.resize((w * s, h * s), Image.LANCZOS)
    out = src.replace(".png", f"_{name}.png")
    c.save(out)
    print("wrote", out, c.size)
