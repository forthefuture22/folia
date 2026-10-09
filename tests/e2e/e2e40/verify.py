#!/usr/bin/env python3
"""e2e40 —— 深色模式下 PDF 里的**位图图片不该被反相**（不该变成负片）。

症状：夜间模式下整页 `.page-canvas` 被 `invert(1) hue-rotate(180deg)` 反相，
"白纸黑字"翻成"黑纸白字"是对的，但页面里的照片/插图跟着一起反 —— 变成负片。

修法：让图片**脱离**那个滤镜的作用范围。渲染完之后，用算子表算出图片所在的位置，
把主 canvas 上那几块的**原色像素**抠进 `.page-image-layer` 里的小 canvas
（详见 `public/pdfjs/bridge.js` 的「深色模式下的图片保护」）。

⚠️ 试过但**行不通**的做法：在同一张 canvas 上对图片区域再反相一次，指望
"反两次 = 原样"。`invert(1) hue-rotate(180deg)` **不是对合** —— 纯红 (255,0,0)
第一次就被顶到 R=1.574 并**截断**，第二次只能得到 (172,63,63)。所以本套件
特意用**纯红**当探针：修好前 (255,146,146) 的粉色、修好后 (255,0,0)，
两态差异极大，判据不含糊。

判据分三层：
  · **结构**：深色下图片层真的建起来了；切浅色清空、切回深色重建。
  · **像素**：深色下图片中心仍是纯红、文字中心被反成白；浅色下两处都是原色。
  · **锚点**：产物 wasm 里确实含 `page-image-layer`（`page_view.rs` 真会渲染这个容器）。

`REGRESS=1` 会把「收集图片四边形」那一步短路掉（判据在 bridge.js 里，可以文本替换），
跑出来应当是 **7/10**：深色那条像素判据 + "建起来了" + "切回深色重建" 变红，
配对判据保持绿。
"""
# --- 路径推导 ---------------------------------------------------------
import pathlib as _pl

_HERE = _pl.Path(__file__).resolve().parent   # tests/e2e/<suite>
_E2E = _HERE.parents[0]                       # tests/e2e
_REPO = _HERE.parents[2]                      # 仓库根
# ---------------------------------------------------------------------
import http.server
import json
import os
import pathlib
import socketserver
import subprocess
import sys
import threading

from PIL import Image

ROOT = pathlib.Path(str(_REPO))
DIST = ROOT / "dist"
WORK = pathlib.Path(str(_HERE))
SITE = WORK / "site"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

REGRESS = os.environ.get("REGRESS") == "1"

# 采样点（页面坐标 = canvas 坐标：`.page` 贴在 (0,0)，画布 200×200 设备像素）。
IMG_PT = (100, 100)   # 红图中心
TXT_PT = (50, 35)     # 黑块中心

# `REGRESS=1`：把「收集图片四边形」这一步短路掉，模拟"没修"的状态。
#
# ⚠️ 锚点必须打在**源头**（收集四边形），不能只关 `renderPage` 里那句
# `paintImageLayer` —— 图片层有**两条**建立路径：渲染完成时、以及主题切回深色时
# （`syncDarkImages`）。只关前者的话，harness 里"切浅色再切回深色"会把图片层重新
# 建起来，截图（最终态）照样是修好的样子，主判据**假绿**。这个坑踩过一次。
REGRESS_PATCHES = [
    (
        "      quads.push(unitSquareQuad(m));",
        "      if (false) quads.push(unitSquareQuad(m));",
    ),
]


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):  # noqa: D102
        pass


def serve(directory: pathlib.Path):
    handler = lambda *a, **kw: QuietHandler(*a, directory=str(directory), **kw)  # noqa: E731
    httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, port


def run_chrome(url: str, extra: list[str], text: bool = False, timeout: int = 180):
    cmd = [
        CHROME,
        "--headless=new",
        "--no-sandbox",
        "--disable-gpu",
        "--hide-scrollbars",
        "--force-device-scale-factor=1",
        "--window-size=200,200",
        *extra,
        url,
    ]
    return subprocess.run(cmd, check=True, capture_output=True, text=text, timeout=timeout)


def build_site() -> None:
    # 沙箱会把 Python 的 `shutil.copytree` 代理到宿主机并拒绝 —— 用 `cp -R`。
    if SITE.exists():
        subprocess.run(["rm", "-rf", str(SITE)], check=True)
    SITE.mkdir(parents=True)
    subprocess.run(["cp", "-R", str(DIST) + "/.", str(SITE)], check=True)

    subprocess.run([sys.executable, str(WORK / "make_pdf.py")], check=True, capture_output=True)
    for name in ("dark.pdf", "page.html", "harness.js"):
        subprocess.run(["cp", str(WORK / name), str(SITE / name)], check=True)

    if REGRESS:
        bridge = SITE / "public/pdfjs/bridge.js"
        text = bridge.read_text()
        for old, new in REGRESS_PATCHES:
            assert old in text, f"REGRESS 锚点没找到：{old!r}"
            text = text.replace(old, new, 1)
        bridge.write_text(text)


def probe(base: str, theme: str):
    """跑一趟：截图（取色）+ dump-dom（读 harness 汇报的结构）。"""
    url = f"{base}/page.html?theme={theme}"
    png = WORK / f"{theme}.png"
    run_chrome(url, [f"--screenshot={png}", "--virtual-time-budget=40000"])
    dom = run_chrome(url, ["--virtual-time-budget=40000", "--dump-dom"], text=True).stdout
    title = dom.split("<title>", 1)[1].split("</title>", 1)[0] if "<title>" in dom else ""
    try:
        result = json.loads(title) if title.startswith("{") else {}
    except ValueError:
        result = {}
    im = Image.open(png).convert("RGB")
    return im.getpixel(IMG_PT), im.getpixel(TXT_PT), result


PASS = 0
FAIL = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
    else:
        FAIL += 1
    tail = f"   ({detail})" if detail else ""
    print(f"[{'PASS' if ok else 'FAIL'}] {label}{tail}")


def is_red(px) -> bool:
    return px[0] > 245 and px[1] < 12 and px[2] < 12


def is_white(px) -> bool:
    return min(px) > 245


def is_black(px) -> bool:
    return max(px) < 12


def main() -> int:
    if not DIST.exists():
        print(f"!! 没有 {DIST}，先跑 `trunk build`")
        return 2

    build_site()
    httpd, port = serve(SITE)
    base = f"http://127.0.0.1:{port}"
    try:
        dark_img, dark_txt, dark = probe(base, "dark")
        light_img, light_txt, light = probe(base, "light")
    finally:
        httpd.shutdown()
        httpd.server_close()

    print(f"深色: 图片{IMG_PT}={dark_img}  文字{TXT_PT}={dark_txt}  {dark}")
    print(f"浅色: 图片{IMG_PT}={light_img}  文字{TXT_PT}={light_txt}  {light}")
    print()

    wasm = sorted(SITE.glob("folia-ui-*_bg.wasm"))
    wasm_has_host = bool(wasm) and b"page-image-layer" in wasm[0].read_bytes()

    # ---- 判据本身成立（否则后面是在量空气） ----
    check(
        "判据本身成立：深色那趟跑到了 done（不是半路卡住）",
        dark.get("stage") == "done",
        f"stage={dark.get('stage')}",
    )
    check(
        "判据本身成立：产物 wasm 里含 page-image-layer（page_view.rs 真会渲染这个容器）",
        wasm_has_host,
        f"wasm={'找到' if wasm else '缺失'}",
    )

    # ---- 主角：深色下图片保持原色、文字照常反相 ----
    check(
        "★ 深色 · 图片中心仍是**纯红**（没被反相成负片）",
        is_red(dark_img),
        f"{dark_img}（没修时是 (255,146,146) 的粉）",
    )
    check(
        "★ 深色 · 文字中心被反成**白**（反相本身还生效）",
        is_white(dark_txt),
        f"{dark_txt}",
    )
    check(
        "★ 深色 · 图片层真的建起来了",
        dark.get("layersDark", 0) >= 1,
        f"layersDark={dark.get('layersDark')}",
    )

    # ---- 主题来回切 ----
    check(
        "★ 切到浅色后图片层被清空（浅色下不占那份内存）",
        dark.get("layersLight") == 0,
        f"layersLight={dark.get('layersLight')}",
    )
    check(
        "★ 切回深色后图片层重建",
        dark.get("layersBack", 0) >= 1,
        f"layersBack={dark.get('layersBack')}",
    )

    # ---- 配对：浅色下一切照旧 ----
    check("配对：浅色 · 图片中心是纯红", is_red(light_img), f"{light_img}")
    check("配对：浅色 · 文字中心保持黑", is_black(light_txt), f"{light_txt}")
    check(
        "配对：浅色 · 不建图片层",
        light.get("layersDark") == 0,
        f"layersDark={light.get('layersDark')}",
    )

    print()
    print(f"{PASS}/{PASS + FAIL} 通过" + ("（REGRESS 对照）" if REGRESS else ""))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
