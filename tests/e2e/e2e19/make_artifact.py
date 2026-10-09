"""把「顶栏改动」拼成一张前后对比图。

BEFORE 用用户给的那张截图（红框是他自己画的，圈的就是要改的那个按钮）；
AFTER 用 e2e19 跑出来的截图。两张都裁到工具条那一行、缩放到同宽。
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

from PIL import Image, ImageDraw, ImageFont

BEFORE_SRC = "/Users/anemone/.workbuddy-ai/clipboard-images/clipboard-2026-10-05T08-01-20-165Z-5a8fc1c9.png"
AFTER_SRC = str(_HERE) + "/toolbar.png"
OUT = pathlib.Path(str(_REPO) + "/.workbuddy-ai/artifacts")
OUT.mkdir(parents=True, exist_ok=True)

W = 760


def font(size):
    # 注意：PingFang.ttc 这个 PIL 打不开（`cannot open resource`），
    # 而它失败后如果直接落到 `load_default()`，中文会全变成豆腐块 ——
    # 所以后面必须留着真正能开的 CJK 字体。
    for path, index in (
        ("/System/Library/Fonts/PingFang.ttc", 2),
        ("/System/Library/Fonts/Hiragino Sans GB.ttc", 0),
        ("/System/Library/Fonts/Supplemental/Arial Unicode.ttf", 0),
    ):
        try:
            return ImageFont.truetype(path, size, index=index)
        except OSError:
            continue
    return ImageFont.load_default()


def fit(im, width):
    h = round(im.height * width / im.width)
    return im.resize((width, h), Image.LANCZOS)


def main():
    before = fit(Image.open(BEFORE_SRC).convert("RGB").crop((70, 20, 1010, 110)), W)
    after = fit(Image.open(AFTER_SRC).convert("RGB").crop((245, 30, 780, 100)), W)

    head = 30
    pad = 14
    canvas = Image.new(
        "RGB", (W + pad * 2, before.height + after.height + head * 2 + pad * 3), (245, 245, 247)
    )
    d = ImageDraw.Draw(canvas)
    f = font(15)

    def label(y, text):
        d.rectangle([pad, y, pad + W, y + head - 4], fill=(28, 28, 32))
        d.text((pad + 10, y + 5), text, fill=(255, 255, 255), font=f)

    y1 = pad
    label(y1, "BEFORE   「工具」带星星  ·  红框按钮是「打开其它 PDF」")
    canvas.paste(before, (pad, y1 + head))

    y2 = y1 + head + before.height + pad
    label(y2, "AFTER   星星删掉 · 按钮改成「展开全部缩略图」· + 在标签栏右端")
    canvas.paste(after, (pad, y2 + head))

    canvas.save(OUT / "toolbar-before-after.png")
    print("wrote", OUT / "toolbar-before-after.png", canvas.size)


if __name__ == "__main__":
    main()
