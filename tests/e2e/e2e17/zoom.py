#!/usr/bin/env python3
"""把截图里的浮层工具栏和选区放大出来，并顺手量一下两者的水平中心是否对齐。

截图是浏览器在 1200x900 窗口下出的（可能被缩放），所以这里量出来的像素只能
和「同一张图里」的选区比 —— 这正是我们想看的：工具栏是不是以选区为中心。
"""

# --- 路径推导 ---------------------------------------------------------
# 原先这里硬编码了 /tmp/e2eNN 和仓库的绝对路径。搬进仓库后改成从本文件
# 位置推导，这样仓库放哪儿都能跑。
import pathlib as _pl
_HERE = _pl.Path(__file__).resolve().parent   # tests/e2e/<suite>
_E2E = _HERE.parents[0]                       # tests/e2e
_REPO = _HERE.parents[2]                      # 仓库根
# ---------------------------------------------------------------------
import pathlib
import sys

from PIL import Image

WORK = pathlib.Path(str(_HERE))


def bbox_of(pred, img, x0, y0, x1, y1):
    px = img.load()
    left, top, right, bottom = None, None, None, None
    for y in range(y0, y1):
        for x in range(x0, x1):
            if pred(px[x, y]):
                if left is None or x < left:
                    left = x
                if right is None or x > right:
                    right = x
                if top is None or y < top:
                    top = y
                if bottom is None or y > bottom:
                    bottom = y
    return None if left is None else (left, top, right, bottom)


def main() -> int:
    name = sys.argv[1] if len(sys.argv) > 1 else "selbar.png"
    src = WORK / name
    img = Image.open(src).convert("RGB")
    w, h = img.size
    print(f"{src.name}: {w} x {h}")

    # 选区是半透明的黄底（mix-blend-mode: multiply 铺在白纸上）：
    # 红绿高、蓝明显低。
    #
    # 但顶部工具栏里那个纯黄色板是同一个色系，会污染检测 —— 所以搜索范围
    # 从 y=150 起（正文第一页从 y≈105 开始，顶栏在 y<100），
    # 横向也从 x=280 起，把左侧栏一起排除。
    X0, Y0 = 280, 150

    def is_sel(c):
        r, g, b = c
        return r > 200 and g > 170 and b < 160 and (r - b) > 70

    sel = bbox_of(is_sel, img, X0, Y0, w, h)
    if not sel:
        print("没找到黄色选区")
        return 1
    print(f"选区 bbox: {sel}  宽 {sel[2] - sel[0]}  高 {sel[3] - sel[1]}")

    # 工具栏的图标是深灰描边、底是纯白，而且正好压在选区上方。
    # 只在「选区上方那一条带子里」找深色像素，避免混进正文。
    band_top = max(Y0, sel[1] - 45)
    band_bottom = sel[1] - 3

    def is_ink(c):
        r, g, b = c
        return r < 140 and g < 140 and b < 140

    bar = bbox_of(is_ink, img, X0, band_top, w, band_bottom)
    if not bar:
        print("工具栏那条带子里没找到深色像素")
        return 1

    # 工具栏是圆角白底、宽度固定；取「图标墨迹」的横向范围当近似，
    # 再往两边各放一点内边距，就接近真实边框。
    pad = 12
    bar_box = (bar[0] - pad, band_top, bar[2] + pad, band_bottom)

    sel_c = (sel[0] + sel[2]) / 2
    bar_c = (bar[0] + bar[2]) / 2
    print(f"图标墨迹 bbox: {bar}  宽 {bar[2] - bar[0]}")
    print(f"选区中心 x = {sel_c:.1f}   工具栏图标中心 x = {bar_c:.1f}   差 {bar_c - sel_c:+.1f}px")

    # 放大裁剪：把工具栏和选区一起框进来
    x0 = max(0, min(bar_box[0], sel[0]) - 20)
    x1 = min(w, max(bar_box[2], sel[2]) + 20)
    y0 = max(0, bar_box[1] - 10)
    y1 = min(h, sel[3] + 14)
    crop = img.crop((x0, y0, x1, y1))
    zoom = crop.resize((crop.width * 3, crop.height * 3), Image.NEAREST)
    out = WORK / f"{src.stem}_zoom.png"
    zoom.save(out)
    print(f"放大图: {out}  ({zoom.width} x {zoom.height}，裁剪自 {x0},{y0} - {x1},{y1})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
