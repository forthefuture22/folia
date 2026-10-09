"""生成两个产物图：

1. `selection-color-before-after.png` —— 选中底色「被容器 opacity 压暗」与「修好后」的对比；
2. `hover-tooltip.png` —— 悬停在标注上时黑底白字批注框的样子。
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

sys.path.insert(0, str(_HERE))
import verify as V  # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

OUT = pathlib.Path(str(_REPO) + "/.workbuddy-ai/artifacts")
OUT.mkdir(parents=True, exist_ok=True)
OLD_CSS = "<style>.textLayer { opacity: 0.25 !important; }</style>"
CROP = (440, 150, 1010, 330)  # (left, top, right, bottom)


def font(size):
    for path, index in (
        ("/System/Library/Fonts/PingFang.ttc", 2),
        ("/System/Library/Fonts/PingFang.ttc", 0),
        ("/System/Library/Fonts/Hiragino Sans GB.ttc", 0),
        ("/System/Library/Fonts/Supplemental/Arial Unicode.ttf", 0),
    ):
        try:
            return ImageFont.truetype(path, size, index=index)
        except OSError:
            continue
    return ImageFont.load_default()


def label(draw, xy, text, f):
    draw.rectangle([xy[0], xy[1], xy[0] + 560, xy[1] + 26], fill=(28, 28, 32))
    draw.text((xy[0] + 8, xy[1] + 6), text, fill=(255, 255, 255), font=f)


def main():
    V.build_site()
    httpd, port = V.serve(V.SITE)
    url = f"http://127.0.0.1:{port}/index.html"
    try:
        dom = V.run_chrome(url + "?shot=selcmp", ["--dump-dom", "--virtual-time-budget=20000"]).stdout
        selcmp = V.extract_result(dom)["selcmp"]

        # 修好后
        V.run_chrome(url + "?shot=selcmp", [f"--screenshot={V.WORK / 'selcmp_after.png'}", "--virtual-time-budget=20000"])
        # 悬停提示
        V.run_chrome(url + "?shot=tip", [f"--screenshot={V.WORK / 'tip.png'}", "--virtual-time-budget=20000"])

        # 旧规则：把 opacity 盖回去
        index = (V.SITE / "index.html").read_text()
        (V.SITE / "index.html").write_text(index.replace("</head>", OLD_CSS + "</head>"))
        V.run_chrome(url + "?shot=selcmp", [f"--screenshot={V.WORK / 'selcmp_before.png'}", "--virtual-time-budget=20000"])
    finally:
        httpd.shutdown()
        httpd.server_close()

    before = Image.open(V.WORK / "selcmp_before.png").convert("RGB").crop(CROP)
    after = Image.open(V.WORK / "selcmp_after.png").convert("RGB").crop(CROP)
    tip = Image.open(V.WORK / "tip.png").convert("RGB")

    # 找一下提示框在图里的位置，好裁一块把标注和提示框都框进去
    from PIL import ImageChops  # noqa: E402

    w = max(before.width, after.width)
    pad, head = 12, 30
    canvas = Image.new(
        "RGB", (w + pad * 2, (before.height + after.height) + head * 2 + pad * 3), (245, 245, 247)
    )
    d = ImageDraw.Draw(canvas)
    f = font(14)
    canvas.paste(before, (pad, head + pad))
    label(d, (pad, pad), "BEFORE  .textLayer { opacity: 0.25 }  —— 选中底色被压暗（很淡）", f)
    y2 = head + pad + before.height + pad
    canvas.paste(after, (pad, y2))
    label(d, (pad, y2 - head), "AFTER   opacity: 1  —— 选中底色 == 高亮底色", f)
    canvas.save(OUT / "selection-color-before-after.png")
    print("wrote", OUT / "selection-color-before-after.png", canvas.size)

    # 提示框：把 page 区域裁出来
    tip_crop = tip.crop((300, 130, 1000, 420))
    tip_crop.save(OUT / "hover-tooltip.png")
    print("wrote", OUT / "hover-tooltip.png", tip_crop.size)
    print("selcmp 取样点:", selcmp)


if __name__ == "__main__":
    main()
