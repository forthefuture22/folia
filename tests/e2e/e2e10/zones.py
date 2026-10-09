#!/usr/bin/env python3
"""在标签栏截图上标出「哪些区域能拖动窗口」。

红色 = 拖拽区（drag=1），蓝色 = 非拖拽区（控件 / 标签块）。
矩形来自 drag.py 导出的真实盒模型，不是手画的。
"""
# --- 路径推导 ---------------------------------------------------------
# 原先这里硬编码了 /tmp/e2eNN 和仓库的绝对路径。搬进仓库后改成从本文件
# 位置推导，这样仓库放哪儿都能跑。
import pathlib as _pl
_HERE = _pl.Path(__file__).resolve().parent   # tests/e2e/<suite>
_E2E = _HERE.parents[0]                       # tests/e2e
_REPO = _HERE.parents[2]                      # 仓库根
# ---------------------------------------------------------------------
import json, pathlib, re, subprocess, sys

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
HARNESS = pathlib.Path(str(_HERE) + "/drag")
PORT = 8792
OUT = pathlib.Path(str(_HERE) + "/drag_zones.png")
DOM = pathlib.Path(str(_HERE) + "/zones.dom.html")
SHOT = pathlib.Path(str(_HERE) + "/zones_raw.png")

# 1) 起服务 + 截图 + dump（必须在同一条命令生命周期里，后台服务会被回收）
subprocess.run([sys.executable, str(_HERE) + "/drag.py"], check=True)
srv = subprocess.Popen(
    [sys.executable, "-m", "http.server", "--directory", str(HARNESS), str(PORT)],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
try:
    import time
    time.sleep(1.2)
    url = f"http://127.0.0.1:{PORT}/"
    common = ["--headless=new", "--disable-gpu", "--no-sandbox",
              "--force-device-scale-factor=2", "--window-size=1080,750",
              "--virtual-time-budget=12000"]
    subprocess.run([CHROME, *common, "--screenshot", str(SHOT), url],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    with open(DOM, "w") as f:
        subprocess.run([CHROME, *common, "--dump-dom", url],
                       stdout=f, stderr=subprocess.DEVNULL, check=True)
finally:
    srv.terminate()

raw = DOM.read_text(encoding="utf-8", errors="replace")
m = re.search(r'<pre id="rects">(.*?)</pre>', raw, re.S)
assert m, "no <pre id='rects'>"
data = json.loads(m.group(1).replace("&quot;", '"').replace("&amp;", "&"))
print("viewport", data["viewport"], "scale", data["scale"])

from PIL import Image, ImageDraw

S = data["scale"]          # 2
img = Image.open(SHOT).convert("RGBA")
print("image", img.size)
ov = Image.new("RGBA", img.size, (0, 0, 0, 0))
d = ImageDraw.Draw(ov)

def box(r, color, label=None):
    if not r:
        return
    x0, y0 = r["x"] * S, r["y"] * S
    x1, y1 = (r["x"] + r["w"]) * S, (r["y"] + r["h"]) * S
    d.rectangle([x0, y0, x1, y1], fill=color, outline=color[:3] + (255,), width=2)

RED = (220, 38, 38, 70)     # 能拖
BLUE = (37, 99, 235, 70)    # 不能拖

bar = data["bar"]
# 整条标签栏先铺一层「能拖」，再把不能拖的部分盖上去
box(bar, RED)
box(data["home"], BLUE)
box(data["add"], BLUE)
box(data["right"], BLUE)
for t in data["tabs"]:
    box(t, BLUE)

img = Image.alpha_composite(img, ov)

# 只在标签栏那一条上做标注，别把整页弄花
d2 = ImageDraw.Draw(img)
top = int(bar["y"] * S) - 30
d2.rectangle([0, top, img.size[0], top + 22], fill=(255, 255, 255, 235))
d2.rectangle([8, top + 6, 20, top + 16], fill=(220, 38, 38, 255))
d2.text((26, top + 6), "draggable (blank tab-bar area)", fill=(20, 20, 20, 255))
d2.rectangle([268, top + 6, 280, top + 16], fill=(37, 99, 235, 255))
d2.text((286, top + 6), "clickable (buttons / tab chips)", fill=(20, 20, 20, 255))

# 裁到标签栏附近，放大看
crop_top = max(0, int(bar["y"] * S) - 40)
crop_bot = min(img.size[1], int((bar["y"] + bar["h"]) * S) + 10)
img = img.crop((0, crop_top, img.size[0], crop_bot))
img.convert("RGB").save(OUT, quality=95)
print("wrote", OUT, img.size)
