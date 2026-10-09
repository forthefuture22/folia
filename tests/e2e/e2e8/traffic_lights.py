#!/usr/bin/env python3
"""在截图上画出「假红绿灯」，用来验证 titleBarStyle=Overlay 的几何位置。

真机上的红绿灯是原生 NSButton，headless Chrome 截不到，
所以按 tao 的算法反推位置再画上去，检查它们是否正好落在标签栏里、且在家按钮左边。

tao::inset_traffic_lights 的语义：
  title_bar_frame_height = 按钮高(16) + y
  只有 origin.x 被改写：x + i * space_between（系统间距 20）
  → 按钮垂直中心距窗口顶部 = y + 2
"""
import sys
from PIL import Image, ImageDraw

SRC = sys.argv[1]
X, Y = 18, 19          # 与 tauri.conf.json 的 trafficLightPosition 一致
SPACING, FRAME_W, D = 20, 14, 12   # 系统间距 / 按钮框宽 / 可见圆直径
COLORS = [(255, 95, 87), (254, 188, 46), (40, 200, 64)]   # 关 / 最小化 / 缩放

img = Image.open(SRC).convert("RGB")
d = ImageDraw.Draw(img)
scale = img.width / 1180          # DPR 2

cy = Y + 2                        # 按钮垂直中心
for i, color in enumerate(COLORS):
    cx = X + i * SPACING + FRAME_W / 2
    box = [((cx - D / 2) * scale, (cy - D / 2) * scale),
           ((cx + D / 2) * scale, (cy + D / 2) * scale)]
    d.ellipse(box, fill=color, outline=(0, 0, 0, 40), width=max(1, int(scale)))
    print(f"light {i}: center=({cx:.0f},{cy}) box={[round(v, 1) for p in box for v in p]}")

out = SRC.replace(".png", "_tl.png")
img.save(out)
print("wrote", out)
